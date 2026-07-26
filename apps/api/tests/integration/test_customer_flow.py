"""End-to-end Customer/CRM flow against a real Postgres database — fixtures
mirror `test_appointment_flow.py`'s/`test_dispatch_flow.py`'s exactly."""

import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.session import Base, engine
from app.main import app, fastapi_app
from tests.fakes import FakeAIProvider, default_reply

_TEST_VAPI_SECRET = "test-vapi-secret"


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


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["tokens"]["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_technician(
    client: AsyncClient, owner_token: str, *, email: str, password: str = "temp-pass-123"
) -> dict:
    response = await client.post(
        "/api/v1/dispatch/technicians",
        json={
            "full_name": "Test Technician",
            "email": email,
            "phone_number": "+15005550006",
            "temporary_password": password,
        },
        headers=_auth_headers(owner_token),
    )
    assert response.status_code == 201
    return response.json()


def _reply_with_phone(**overrides):
    kwargs = dict(
        message_to_customer="Thanks for calling — how else can I help?",
        classification=CallClassification.NON_EMERGENCY,
        recommended_action=RecommendedAction.ANSWER_FAQ,
        customer_name="Jane Doe",
        customer_phone="+15551234567",
        customer_address="123 Main St",
        summary="Routine inquiry.",
    )
    kwargs.update(overrides)
    return default_reply(**kwargs)


def _book_appointment_reply(**overrides):
    kwargs = dict(recommended_action=RecommendedAction.BOOK_APPOINTMENT)
    kwargs.update(overrides)
    return _reply_with_phone(**kwargs)


async def _send_message(client: AsyncClient, token: str, message: str, conversation_id=None):
    if conversation_id is None:
        start_resp = await client.post(
            "/api/v1/ai/conversations", json={}, headers=_auth_headers(token)
        )
        assert start_resp.status_code == 201
        conversation_id = start_resp.json()["id"]
    send_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": message},
        headers=_auth_headers(token),
    )
    assert send_resp.status_code == 200
    return conversation_id


@pytest.mark.asyncio(loop_scope="session")
async def test_conversation_with_phone_creates_customer_even_without_ticket_or_appointment(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Customer Flow Org A", "owner-cust-a@example.com")

    fake_ai_provider.queue_reply(_reply_with_phone())
    await _send_message(client, owner_token, "What are your hours?")

    customers_resp = await client.get("/api/v1/customers", headers=_auth_headers(owner_token))
    assert customers_resp.status_code == 200
    customers = customers_resp.json()
    assert len(customers) == 1
    assert customers[0]["full_name"] == "Jane Doe"
    assert customers[0]["phone_number"] == "+15551234567"


@pytest.mark.asyncio(loop_scope="session")
async def test_book_appointment_outcome_links_customer_to_appointment(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Customer Flow Org B", "owner-cust-b@example.com")

    fake_ai_provider.queue_reply(_book_appointment_reply())
    await _send_message(client, owner_token, "Can I book a tune-up?")

    customers = (
        await client.get("/api/v1/customers", headers=_auth_headers(owner_token))
    ).json()
    assert len(customers) == 1
    customer_id = customers[0]["id"]

    history_resp = await client.get(
        f"/api/v1/customers/{customer_id}", headers=_auth_headers(owner_token)
    )
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert history["customer"]["id"] == customer_id
    assert len(history["appointments"]) == 1
    assert history["appointments"][0]["customer_id"] == customer_id
    assert history["tickets"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_repeat_caller_matches_existing_customer(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Customer Flow Org C", "owner-cust-c@example.com")

    fake_ai_provider.queue_reply(_reply_with_phone())
    await _send_message(client, owner_token, "What are your hours?")

    fake_ai_provider.queue_reply(_book_appointment_reply())
    await _send_message(client, owner_token, "Actually can I book a tune-up?")

    customers = (
        await client.get("/api/v1/customers", headers=_auth_headers(owner_token))
    ).json()
    assert len(customers) == 1

    history = (
        await client.get(
            f"/api/v1/customers/{customers[0]['id']}", headers=_auth_headers(owner_token)
        )
    ).json()
    assert len(history["appointments"]) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_manual_customer_create_requires_customers_manage(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Customer Flow Org D", "owner-cust-d@example.com")
    await _create_technician(client, owner_token, email="tech-cust-d@example.com")
    tech_token = await _login(client, "tech-cust-d@example.com", "temp-pass-123")

    # A Technician holds customers:read, not customers:manage — creating a
    # customer by hand is Owner/Admin-only.
    forbidden_create = await client.post(
        "/api/v1/customers",
        json={"full_name": "Walk-in Wanda", "phone_number": "+15559876543"},
        headers=_auth_headers(tech_token),
    )
    assert forbidden_create.status_code == 403

    # But a Technician can still list/read customers.
    readable = await client.get("/api/v1/customers", headers=_auth_headers(tech_token))
    assert readable.status_code == 200

    create_resp = await client.post(
        "/api/v1/customers",
        json={"full_name": "Walk-in Wanda", "phone_number": "+15559876543"},
        headers=_auth_headers(owner_token),
    )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()["id"]

    update_resp = await client.patch(
        f"/api/v1/customers/{customer_id}",
        json={
            "full_name": "Wanda Walker",
            "phone_number": "+15559876543",
            "email": "wanda@example.com",
        },
        headers=_auth_headers(owner_token),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["full_name"] == "Wanda Walker"


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation_across_customers(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_a, _ = await _register(client, "Customer Flow Org E", "owner-cust-e@example.com")
    owner_b, _ = await _register(client, "Customer Flow Org F", "owner-cust-f@example.com")

    fake_ai_provider.queue_reply(_reply_with_phone())
    await _send_message(client, owner_a, "What are your hours?")

    org_a_customers = await client.get("/api/v1/customers", headers=_auth_headers(owner_a))
    org_b_customers = await client.get("/api/v1/customers", headers=_auth_headers(owner_b))
    assert len(org_a_customers.json()) == 1
    assert org_b_customers.json() == []

    customer_id = org_a_customers.json()[0]["id"]
    cross_tenant_get = await client.get(
        f"/api/v1/customers/{customer_id}", headers=_auth_headers(owner_b)
    )
    assert cross_tenant_get.status_code == 404
