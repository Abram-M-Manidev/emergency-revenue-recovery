"""P5 through the real Vapi webhook: a caller ID arriving on the wire, an
association captured by the customer sync, and a later call from the same
line grounded in the AI Brain.

This is the scenario the first real PSTN call exposed — Lucky, a customer
the system already held, asked again for their name and address — reproduced
end to end with a fake model and a real database.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import Depends, Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_provider, get_customer_service, verify_vapi_secret
from app.application.services.customer_service import CustomerService
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import InvalidTokenError
from app.domain.repositories.caller_identity_repository import CallerIdentityRepository
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import (
    SqlAlchemyAppointmentRepository,
    SqlAlchemyCallerIdentityRepository,
    SqlAlchemyConversationOutcomeRepository,
    SqlAlchemyCustomerRepository,
    SqlAlchemyEmergencyTicketRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine, get_db
from app.main import app, fastapi_app
from tests.fakes import FakeAIProvider, default_reply

_TEST_VAPI_SECRET = "test-vapi-secret-p5"
_ASSISTANT_ID = "asst_p5_known_caller"
_CALLER = "+919999999999"


def _verify_vapi_secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _TEST_VAPI_SECRET:
        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


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
async def fake_ai_provider():
    provider = FakeAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: provider
    fastapi_app.dependency_overrides[verify_vapi_secret] = _verify_vapi_secret_override
    yield provider
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready, fake_ai_provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient, org_name: str, email: str):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Test Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    return uuid.UUID(body["user"]["organization_id"])


async def _seed_voice_line(organization_id: uuid.UUID, *, assistant_id: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=organization_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number="+15005550006",
                is_active=True,
            )
        )
        await session.commit()


def _reply(**overrides):
    kwargs = dict(
        message_to_customer="A technician is on the way.",
        classification=CallClassification.EMERGENCY,
        recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        customer_name="Lucky",
        customer_phone="123456789",
        customer_address="16 Street, California",
        summary="No heat.",
    )
    kwargs.update(overrides)
    return default_reply(**kwargs)


async def _call(client, *, call_id, assistant_id, caller=_CALLER, utterance="No heat."):
    return await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {
                "id": call_id,
                "assistantId": assistant_id,
                "customer": {"number": caller} if caller else None,
            },
            "messages": [{"role": "user", "content": utterance}],
        },
        headers={"x-vapi-secret": _TEST_VAPI_SECRET},
    )


async def _associations(org_id: uuid.UUID) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT i.caller_number, c.full_name FROM customer_caller_identities i "
                "JOIN customers c ON c.id = i.customer_id WHERE i.organization_id = :org"
            ),
            {"org": org_id},
        )
        return [(r[0], r[1]) for r in rows.all()]


@pytest.mark.asyncio(loop_scope="session")
async def test_first_call_captures_the_association_and_second_call_is_grounded(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    """The Lucky scenario, end to end."""
    assistant = _ASSISTANT_ID + "-a"
    org_id = await _register(client, "P5 Org A", "owner-p5-a@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)

    # First call: nothing known yet, so the prompt must be ungrounded.
    fake_ai_provider.queue_reply(_reply())
    assert (await _call(client, call_id="p5_call_1", assistant_id=assistant)).status_code == 200
    assert "Caller records" not in fake_ai_provider.requests[-1].system_prompt

    # The association was captured by the customer sync.
    assert await _associations(org_id) == [(_CALLER, "Lucky")]

    # Second call from the same line is recognised.
    fake_ai_provider.queue_reply(_reply())
    assert (await _call(client, call_id="p5_call_2", assistant_id=assistant)).status_code == 200
    prompt = fake_ai_provider.requests[-1].system_prompt

    assert "Caller records" in prompt
    assert "Lucky" in prompt
    assert "address on file" in prompt
    # The security decision: the stored address is never recited.
    assert "16 Street" not in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_call_with_no_caller_id_captures_nothing_and_grounds_nothing(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant = _ASSISTANT_ID + "-b"
    org_id = await _register(client, "P5 Org B", "owner-p5-b@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)

    fake_ai_provider.queue_reply(_reply())
    assert (
        await _call(client, call_id="p5_call_3", assistant_id=assistant, caller=None)
    ).status_code == 200

    assert await _associations(org_id) == []
    assert "Caller records" not in fake_ai_provider.requests[-1].system_prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_a_caller_id_from_another_tenant_does_not_ground(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    assistant_c = _ASSISTANT_ID + "-c"
    assistant_d = _ASSISTANT_ID + "-d"
    org_c = await _register(client, "P5 Org C", "owner-p5-c@example.com")
    org_d = await _register(client, "P5 Org D", "owner-p5-d@example.com")
    await _seed_voice_line(org_c, assistant_id=assistant_c)
    await _seed_voice_line(org_d, assistant_id=assistant_d)

    # Org C learns this caller.
    fake_ai_provider.queue_reply(_reply())
    await _call(client, call_id="p5_call_4", assistant_id=assistant_c)
    assert await _associations(org_c) == [(_CALLER, "Lucky")]

    # The same line calling org D is a stranger there.
    fake_ai_provider.queue_reply(_reply(customer_name="Someone Else", customer_phone="999"))
    await _call(client, call_id="p5_call_5", assistant_id=assistant_d)
    prompt = fake_ai_provider.requests[-1].system_prompt

    assert "Caller records" not in prompt
    assert "Lucky" not in prompt


@pytest.mark.asyncio(loop_scope="session")
async def test_shared_line_stops_grounding_once_a_second_customer_appears(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    """One customer on the line grounds; two must not."""
    assistant = _ASSISTANT_ID + "-e"
    org_id = await _register(client, "P5 Org E", "owner-p5-e@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)

    fake_ai_provider.queue_reply(_reply(customer_name="Alice", customer_phone="111"))
    await _call(client, call_id="p5_call_6", assistant_id=assistant)

    fake_ai_provider.queue_reply(_reply(customer_name="Alice", customer_phone="111"))
    await _call(client, call_id="p5_call_7", assistant_id=assistant)
    assert "Alice" in fake_ai_provider.requests[-1].system_prompt

    # A second person answers the same line.
    fake_ai_provider.queue_reply(_reply(customer_name="Bob", customer_phone="222"))
    await _call(client, call_id="p5_call_8", assistant_id=assistant)

    fake_ai_provider.queue_reply(_reply(customer_name="Bob", customer_phone="222"))
    await _call(client, call_id="p5_call_9", assistant_id=assistant)
    prompt = fake_ai_provider.requests[-1].system_prompt

    assert "Caller records" not in prompt
    assert "Alice" not in prompt
    assert "Bob" not in prompt


# --- P5 blocker fix: a failing association must not break a live turn ---


@pytest_asyncio.fixture(loop_scope="session")
async def failing_association(fake_ai_provider):
    """Replaces only the association *write* with one that always raises,
    leaving every other repository real. This is the exact shape of the
    blocker: the last statement of the outcome sync fails after C1 and the
    ticket link have already succeeded."""

    class _FailingAssociation(CallerIdentityRepository):
        def __init__(self, inner: SqlAlchemyCallerIdentityRepository) -> None:
            self._inner = inner

        async def find_customers_by_caller_number(self, organization_id, caller_number):
            return await self._inner.find_customers_by_caller_number(
                organization_id, caller_number
            )

        async def associate(self, organization_id, *, customer_id, caller_number):
            raise RuntimeError("association storage unavailable")

    def _override(db: AsyncSession = Depends(get_db)) -> CustomerService:
        return CustomerService(
            customer_repository=SqlAlchemyCustomerRepository(db),
            conversation_outcome_repository=SqlAlchemyConversationOutcomeRepository(db),
            emergency_ticket_repository=SqlAlchemyEmergencyTicketRepository(db),
            appointment_repository=SqlAlchemyAppointmentRepository(db),
            caller_identity_repository=_FailingAssociation(
                SqlAlchemyCallerIdentityRepository(db)
            ),
        )

    fastapi_app.dependency_overrides[get_customer_service] = _override
    yield
    fastapi_app.dependency_overrides.pop(get_customer_service, None)


@pytest.mark.asyncio(loop_scope="session")
async def test_streaming_turn_completes_when_association_capture_fails(
    client: AsyncClient, fake_ai_provider: FakeAIProvider, failing_association
):
    """The invariant the blocker threatened: the caller still gets a
    terminated stream and a hang-up, and the emergency ticket survives."""
    assistant = _ASSISTANT_ID + "-fail"
    org_id = await _register(client, "P5 Org F", "owner-p5-f@example.com")
    await _seed_voice_line(org_id, assistant_id=assistant)

    fake_ai_provider.queue_reply(_reply(is_conversation_complete=True))
    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {
                "id": "p5_call_fail",
                "assistantId": assistant,
                "customer": {"number": _CALLER},
            },
            "messages": [{"role": "user", "content": "No heat."}],
            "stream": True,
        },
        headers={"x-vapi-secret": _TEST_VAPI_SECRET},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # The stream terminated and the call was ended — neither happens if the
    # association exception escapes.
    assert response.text.count("data: [DONE]") == 1
    assert "endCall" in response.text
    assert "A technician is on the way." in response.text

    async with AsyncSessionLocal() as session:
        ticket_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM emergency_tickets WHERE organization_id = :org"
                ),
                {"org": org_id},
            )
        ).scalar_one()
        customer_count = (
            await session.execute(
                text("SELECT count(*) FROM customers WHERE organization_id = :org"),
                {"org": org_id},
            )
        ).scalar_one()

    # The valuable work all committed despite the failed association.
    assert ticket_count == 1
    assert customer_count == 1
    # ...and nothing was recorded as an association.
    assert await _associations(org_id) == []
