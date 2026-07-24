"""End-to-end AI Brain conversation flow against a real Postgres database.

The AIProvider dependency is overridden with FakeAIProvider so this test
never makes a real, paid, non-deterministic OpenAI call — see
test_ai_conversation_flow.py's sibling `test_openai_provider_smoke.py` for
the one test that does, gated behind OPENAI_API_KEY being set.

Requires DATABASE_URL (see tests/conftest.py) to point at a reachable,
disposable database. Skips gracefully if no database is reachable, same as
test_auth_flow.py."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.deps import get_ai_provider
from app.core.config import get_settings
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.role import RoleModel
from app.infrastructure.database.repositories import SqlAlchemyUserRepository
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.infrastructure.security.jwt import create_access_token
from app.infrastructure.security.password import hash_password
from app.main import app, fastapi_app
from tests.fakes import FakeAIProvider, default_reply


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
    yield provider
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready, fake_ai_provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient, org_name: str, email: str) -> tuple[str, uuid.UUID]:
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
    return body["tokens"]["access_token"], uuid.UUID(body["user"]["organization_id"])


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio(loop_scope="session")
async def test_emergency_conversation_flow(client: AsyncClient, fake_ai_provider: FakeAIProvider):
    token, _ = await _register(client, "Acme HVAC", "owner-ai-a@example.com")
    headers = _auth_headers(token)

    fake_ai_provider.queue_reply(
        default_reply(
            message_to_customer="That sounds urgent — help is on the way.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_name="Jane Doe",
            customer_phone="+15551234567",
            is_conversation_complete=True,
        )
    )

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={"caller_phone_number": "+15551234567"}, headers=headers
    )
    assert start_resp.status_code == 201
    conversation_id = start_resp.json()["id"]
    assert start_resp.json()["status"] == "active"

    send_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "My basement is flooding!"},
        headers=headers,
    )
    assert send_resp.status_code == 200
    body = send_resp.json()
    assert body["outcome"]["classification"] == "emergency"
    assert body["outcome"]["recommended_action"] == "create_emergency_ticket"
    assert body["outcome"]["customer_name"] == "Jane Doe"
    assert body["conversation"]["status"] == "completed"

    detail_resp = await client.get(f"/api/v1/ai/conversations/{conversation_id}", headers=headers)
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert len(detail["messages"]) == 2
    assert detail["outcome"]["classification"] == "emergency"

    # Conversation already completed: another message is rejected.
    followup_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Are you still there?"},
        headers=headers,
    )
    assert followup_resp.status_code == 409


@pytest.mark.asyncio(loop_scope="session")
async def test_non_emergency_conversation_flow(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token, _ = await _register(client, "Bravo Plumbing", "owner-ai-b@example.com")
    headers = _auth_headers(token)

    fake_ai_provider.queue_reply(
        default_reply(
            message_to_customer="We're open 8am-5pm Monday through Friday.",
            classification=CallClassification.NON_EMERGENCY,
            recommended_action=RecommendedAction.ANSWER_FAQ,
        )
    )

    start_resp = await client.post("/api/v1/ai/conversations", json={}, headers=headers)
    assert start_resp.status_code == 201
    conversation_id = start_resp.json()["id"]

    send_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "What are your hours?"},
        headers=headers,
    )
    assert send_resp.status_code == 200
    body = send_resp.json()
    assert body["outcome"]["classification"] == "non_emergency"
    assert body["conversation"]["status"] == "active"

    list_resp = await client.get("/api/v1/ai/conversations", headers=headers)
    assert list_resp.status_code == 200
    assert any(c["id"] == conversation_id for c in list_resp.json())


@pytest.mark.asyncio(loop_scope="session")
async def test_cross_tenant_conversation_access_is_isolated(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    token_a, _ = await _register(client, "Org Alpha AI", "owner-ai-alpha@example.com")
    token_b, _ = await _register(client, "Org Bravo AI", "owner-ai-bravo@example.com")
    headers_a = _auth_headers(token_a)
    headers_b = _auth_headers(token_b)

    start_resp = await client.post("/api/v1/ai/conversations", json={}, headers=headers_a)
    conversation_id = start_resp.json()["id"]

    get_attempt = await client.get(f"/api/v1/ai/conversations/{conversation_id}", headers=headers_b)
    assert get_attempt.status_code == 404

    send_attempt = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Hello?"},
        headers=headers_b,
    )
    assert send_attempt.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_member_role_can_read_but_not_simulate(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    _, org_id = await _register(client, "Org Charlie AI", "owner-ai-charlie@example.com")
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RoleModel).where(RoleModel.organization_id == org_id, RoleModel.name == "Member")
        )
        member_role = result.scalar_one()

        user_repo = SqlAlchemyUserRepository(session)
        member_user = await user_repo.create(
            organization_id=org_id,
            email="member-ai-charlie@example.com",
            hashed_password=hash_password("super-secret-123"),
            full_name="Member User",
            role_ids=[member_role.id],
        )
        await session.commit()

    member_token = create_access_token(
        user_id=member_user.id,
        organization_id=member_user.organization_id,
        permissions=member_user.permission_codes,
        is_superuser=False,
        settings=settings,
    )
    member_headers = _auth_headers(member_token)

    list_resp = await client.get("/api/v1/ai/conversations", headers=member_headers)
    assert list_resp.status_code == 200

    start_attempt = await client.post("/api/v1/ai/conversations", json={}, headers=member_headers)
    assert start_attempt.status_code == 403
