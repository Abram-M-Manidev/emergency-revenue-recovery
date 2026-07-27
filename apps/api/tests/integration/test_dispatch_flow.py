"""End-to-end Emergency Dispatch flow against a real Postgres database —
fixtures mirror `test_voice_webhook_flow.py`'s exactly."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_ai_provider
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.session import Base, engine
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


@pytest.mark.asyncio(loop_scope="session")
async def test_emergency_outcome_creates_ticket_visible_in_queue(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(client, "Dispatch Flow Org A", "owner-dispatch-a@example.com")

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_token)
    )
    assert start_resp.status_code == 201
    conversation_id = start_resp.json()["id"]

    fake_ai_provider.queue_reply(
        default_reply(
            message_to_customer="Help is on the way.",
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_name="Jane Doe",
            customer_address="123 Main St",
            summary="Basement flooding.",
        )
    )
    send_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "My basement is flooding!"},
        headers=_auth_headers(owner_token),
    )
    assert send_resp.status_code == 200

    tickets_resp = await client.get("/api/v1/dispatch/tickets", headers=_auth_headers(owner_token))
    assert tickets_resp.status_code == 200
    tickets = tickets_resp.json()
    assert len(tickets) == 1
    assert tickets[0]["status"] == "new"
    assert tickets[0]["customer_name"] == "Jane Doe"
    assert tickets[0]["conversation_id"] == conversation_id

    # Repeating a non-emergency turn on the same conversation must not spawn
    # a second ticket.
    fake_ai_provider.queue_reply(default_reply())
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Thanks."},
        headers=_auth_headers(owner_token),
    )
    tickets_after = await client.get(
        "/api/v1/dispatch/tickets", headers=_auth_headers(owner_token)
    )
    assert len(tickets_after.json()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_assign_and_resolve_ticket_as_the_assigned_technician(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(client, "Dispatch Flow Org B", "owner-dispatch-b@example.com")
    technician = await _create_technician(
        client, owner_token, email="tech-b@example.com", password="temp-pass-123"
    )
    await _create_technician(
        client, owner_token, email="tech-b2@example.com", password="temp-pass-123"
    )
    tech_token = await _login(client, "tech-b@example.com", "temp-pass-123")
    other_tech_token = await _login(client, "tech-b2@example.com", "temp-pass-123")

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_token)
    )
    conversation_id = start_resp.json()["id"]
    fake_ai_provider.queue_reply(
        default_reply(
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Gas leak!"},
        headers=_auth_headers(owner_token),
    )
    ticket = (
        await client.get("/api/v1/dispatch/tickets", headers=_auth_headers(owner_token))
    ).json()[0]
    ticket_id = ticket["id"]

    # A Technician holds dispatch:read + dispatch:update_assigned, not
    # dispatch:manage — assigning tickets is Owner/Admin-only.
    forbidden_assign = await client.post(
        f"/api/v1/dispatch/tickets/{ticket_id}/assign",
        json={"technician_user_id": technician["user_id"]},
        headers=_auth_headers(tech_token),
    )
    assert forbidden_assign.status_code == 403

    assign_resp = await client.post(
        f"/api/v1/dispatch/tickets/{ticket_id}/assign",
        json={"technician_user_id": technician["user_id"]},
        headers=_auth_headers(owner_token),
    )
    assert assign_resp.status_code == 200
    assert assign_resp.json()["status"] == "assigned"

    # A technician the ticket is not assigned to cannot update its status.
    forbidden_status = await client.post(
        f"/api/v1/dispatch/tickets/{ticket_id}/status",
        json={"status": "en_route"},
        headers=_auth_headers(other_tech_token),
    )
    assert forbidden_status.status_code == 403

    en_route_resp = await client.post(
        f"/api/v1/dispatch/tickets/{ticket_id}/status",
        json={"status": "en_route"},
        headers=_auth_headers(tech_token),
    )
    assert en_route_resp.status_code == 200
    assert en_route_resp.json()["status"] == "en_route"

    resolved_resp = await client.post(
        f"/api/v1/dispatch/tickets/{ticket_id}/status",
        json={"status": "resolved"},
        headers=_auth_headers(tech_token),
    )
    assert resolved_resp.status_code == 200
    assert resolved_resp.json()["status"] == "resolved"
    assert resolved_resp.json()["closed_at"] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation_across_tickets_and_technicians(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_a, _ = await _register(client, "Dispatch Flow Org C", "owner-dispatch-c@example.com")
    owner_b, _ = await _register(client, "Dispatch Flow Org D", "owner-dispatch-d@example.com")

    await _create_technician(client, owner_a, email="tech-c@example.com")

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_a)
    )
    conversation_id = start_resp.json()["id"]
    fake_ai_provider.queue_reply(
        default_reply(
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
        )
    )
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Electrical fire!"},
        headers=_auth_headers(owner_a),
    )

    org_a_tickets = await client.get("/api/v1/dispatch/tickets", headers=_auth_headers(owner_a))
    org_b_tickets = await client.get("/api/v1/dispatch/tickets", headers=_auth_headers(owner_b))
    assert len(org_a_tickets.json()) == 1
    assert org_b_tickets.json() == []

    org_a_technicians = await client.get(
        "/api/v1/dispatch/technicians", headers=_auth_headers(owner_a)
    )
    org_b_technicians = await client.get(
        "/api/v1/dispatch/technicians", headers=_auth_headers(owner_b)
    )
    assert len(org_a_technicians.json()) == 1
    assert org_b_technicians.json() == []

    ticket_id = org_a_tickets.json()[0]["id"]
    cross_tenant_get = await client.get(
        f"/api/v1/dispatch/tickets/{ticket_id}", headers=_auth_headers(owner_b)
    )
    assert cross_tenant_get.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_technician_email_is_rejected(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Dispatch Flow Org E", "owner-dispatch-e@example.com")
    await _create_technician(client, owner_token, email="dup-tech@example.com")

    response = await client.post(
        "/api/v1/dispatch/technicians",
        json={
            "full_name": "Duplicate",
            "email": "dup-tech@example.com",
            "phone_number": "+15005550006",
            "temporary_password": "temp-pass-123",
        },
        headers=_auth_headers(owner_token),
    )
    assert response.status_code == 409
