"""End-to-end Analytics flow against a real Postgres database — fixtures
mirror `test_customer_flow.py`'s/`test_dispatch_flow.py`'s exactly. Also
covers the Milestone 8 revenue-tracking additions to Business Knowledge
(`Service.default_price`) and Dispatch/Appointments (`actual_value` on the
status-update endpoints), since those are what feed the numbers this
endpoint reports."""

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


async def _send_message(client: AsyncClient, token: str, message: str):
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
async def test_analytics_summary_reports_calls_tickets_appointments_and_revenue(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Analytics Flow Org A", "owner-an-a@example.com")
    technician = await _create_technician(client, owner_token, email="tech-an-a@example.com")
    tech_token = await _login(client, "tech-an-a@example.com", "temp-pass-123")

    # One emergency call -> ticket, resolved with a captured value.
    fake_ai_provider.queue_reply(
        default_reply(
            classification=CallClassification.EMERGENCY,
            recommended_action=RecommendedAction.CREATE_EMERGENCY_TICKET,
            customer_phone="+15551234567",
            customer_name="Jane Doe",
        )
    )
    await _send_message(client, owner_token, "Gas leak!")
    ticket = (
        await client.get("/api/v1/dispatch/tickets", headers=_auth_headers(owner_token))
    ).json()[0]
    await client.post(
        f"/api/v1/dispatch/tickets/{ticket['id']}/assign",
        json={"technician_user_id": technician["user_id"]},
        headers=_auth_headers(owner_token),
    )
    await client.post(
        f"/api/v1/dispatch/tickets/{ticket['id']}/status",
        json={"status": "en_route"},
        headers=_auth_headers(tech_token),
    )
    resolved_resp = await client.post(
        f"/api/v1/dispatch/tickets/{ticket['id']}/status",
        json={"status": "resolved", "actual_value": 250.00},
        headers=_auth_headers(tech_token),
    )
    assert resolved_resp.status_code == 200
    assert resolved_resp.json()["actual_value"] == 250.0

    # One non-emergency call -> appointment, completed with a captured value.
    fake_ai_provider.queue_reply(
        default_reply(
            classification=CallClassification.NON_EMERGENCY,
            recommended_action=RecommendedAction.BOOK_APPOINTMENT,
            customer_phone="+15559876543",
            customer_name="John Roe",
        )
    )
    await _send_message(client, owner_token, "Can I book a tune-up?")
    appointment = (
        await client.get("/api/v1/appointments", headers=_auth_headers(owner_token))
    ).json()[0]
    await client.post(
        f"/api/v1/appointments/{appointment['id']}/schedule",
        json={
            "scheduled_start_at": "2026-06-01T15:00:00Z",
            "duration_minutes": 45,
            "technician_user_id": technician["user_id"],
        },
        headers=_auth_headers(owner_token),
    )
    completed_resp = await client.post(
        f"/api/v1/appointments/{appointment['id']}/status",
        json={"status": "completed", "actual_value": 125.5},
        headers=_auth_headers(tech_token),
    )
    assert completed_resp.status_code == 200
    assert completed_resp.json()["actual_value"] == 125.5

    summary_resp = await client.get(
        "/api/v1/analytics/summary?range=all", headers=_auth_headers(owner_token)
    )
    assert summary_resp.status_code == 200
    summary = summary_resp.json()

    assert summary["total_conversations"] == 2
    assert summary["tickets_created"] == 1
    assert summary["tickets_resolved"] == 1
    assert summary["appointments_created"] == 1
    assert summary["appointments_completed"] == 1
    assert summary["new_customers"] == 2
    assert summary["total_customers"] == 2
    assert summary["ticket_revenue"] == 250.0
    assert summary["appointment_revenue"] == 125.5
    assert summary["total_revenue"] == 375.5
    assert sum(entry["amount"] for entry in summary["revenue_by_day"]) == pytest.approx(375.5)
    classifications = {b["label"]: b["count"] for b in summary["classification_breakdown"]}
    assert classifications == {"emergency": 1, "non_emergency": 1}


@pytest.mark.asyncio(loop_scope="session")
async def test_analytics_requires_analytics_read_permission(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, _ = await _register(client, "Analytics Flow Org B", "owner-an-b@example.com")
    await _create_technician(client, owner_token, email="tech-an-b@example.com")
    tech_token = await _login(client, "tech-an-b@example.com", "temp-pass-123")

    # Technician does not hold analytics:read (business-insight data, not
    # an operational/job-execution permission — see rbac.py).
    forbidden = await client.get(
        "/api/v1/analytics/summary", headers=_auth_headers(tech_token)
    )
    assert forbidden.status_code == 403

    allowed = await client.get(
        "/api/v1/analytics/summary", headers=_auth_headers(owner_token)
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio(loop_scope="session")
async def test_analytics_is_tenant_isolated(client: AsyncClient, fake_ai_provider: FakeAIProvider):
    owner_a, _ = await _register(client, "Analytics Flow Org C", "owner-an-c@example.com")
    owner_b, _ = await _register(client, "Analytics Flow Org D", "owner-an-d@example.com")

    fake_ai_provider.queue_reply(
        default_reply(
            classification=CallClassification.NON_EMERGENCY,
            recommended_action=RecommendedAction.ANSWER_FAQ,
        )
    )
    await _send_message(client, owner_a, "What are your hours?")

    summary_a = (
        await client.get("/api/v1/analytics/summary?range=all", headers=_auth_headers(owner_a))
    ).json()
    summary_b = (
        await client.get("/api/v1/analytics/summary?range=all", headers=_auth_headers(owner_b))
    ).json()

    assert summary_a["total_conversations"] == 1
    assert summary_b["total_conversations"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_service_default_price_round_trips(client: AsyncClient, fake_ai_provider: FakeAIProvider):
    owner_token, _ = await _register(client, "Analytics Flow Org E", "owner-an-e@example.com")

    create_resp = await client.post(
        "/api/v1/business-knowledge/services",
        json={
            "name": "Furnace Tune-Up",
            "description": None,
            "category": "Maintenance",
            "is_emergency_eligible": False,
            "is_active": True,
            "default_duration_minutes": 60,
            "default_price": 149.99,
        },
        headers=_auth_headers(owner_token),
    )
    assert create_resp.status_code == 201
    assert create_resp.json()["default_price"] == 149.99

    service_id = create_resp.json()["id"]
    update_resp = await client.patch(
        f"/api/v1/business-knowledge/services/{service_id}",
        json={
            "name": "Furnace Tune-Up",
            "description": None,
            "category": "Maintenance",
            "is_emergency_eligible": False,
            "is_active": True,
            "default_duration_minutes": 60,
            "default_price": 179.5,
        },
        headers=_auth_headers(owner_token),
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["default_price"] == 179.5

    list_resp = await client.get(
        "/api/v1/business-knowledge/services", headers=_auth_headers(owner_token)
    )
    assert list_resp.json()[0]["default_price"] == 179.5
