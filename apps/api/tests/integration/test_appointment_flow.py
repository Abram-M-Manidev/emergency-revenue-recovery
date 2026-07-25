"""End-to-end Appointment Management flow against a real Postgres database —
fixtures mirror `test_dispatch_flow.py`'s and `test_voice_webhook_flow.py`'s
exactly."""

import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
from app.domain.entities.voice_line import VoiceProvider
from app.domain.exceptions import InvalidTokenError
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
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


def _vapi_headers() -> dict[str, str]:
    return {"x-vapi-secret": _TEST_VAPI_SECRET}


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


def _book_appointment_reply(**overrides):
    kwargs = dict(
        message_to_customer="I'd be happy to get that scheduled.",
        classification=CallClassification.NON_EMERGENCY,
        recommended_action=RecommendedAction.BOOK_APPOINTMENT,
        customer_name="Jane Doe",
        customer_address="123 Main St",
        summary="Wants a routine furnace tune-up.",
    )
    kwargs.update(overrides)
    return default_reply(**kwargs)


@pytest.mark.asyncio(loop_scope="session")
async def test_book_appointment_outcome_creates_appointment_visible_in_queue(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(
        client, "Appointment Flow Org A", "owner-appt-a@example.com"
    )

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_token)
    )
    assert start_resp.status_code == 201
    conversation_id = start_resp.json()["id"]

    fake_ai_provider.queue_reply(_book_appointment_reply())
    send_resp = await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Can I book a tune-up?"},
        headers=_auth_headers(owner_token),
    )
    assert send_resp.status_code == 200

    appointments_resp = await client.get(
        "/api/v1/appointments", headers=_auth_headers(owner_token)
    )
    assert appointments_resp.status_code == 200
    appointments = appointments_resp.json()
    assert len(appointments) == 1
    assert appointments[0]["status"] == "requested"
    assert appointments[0]["customer_name"] == "Jane Doe"
    assert appointments[0]["conversation_id"] == conversation_id

    # Repeating a turn on the same conversation must not spawn a second
    # appointment.
    fake_ai_provider.queue_reply(default_reply())
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Thanks."},
        headers=_auth_headers(owner_token),
    )
    appointments_after = await client.get(
        "/api/v1/appointments", headers=_auth_headers(owner_token)
    )
    assert len(appointments_after.json()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_schedule_and_complete_appointment_as_assigned_technician(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(
        client, "Appointment Flow Org B", "owner-appt-b@example.com"
    )
    technician = await _create_technician(
        client, owner_token, email="tech-appt-b@example.com", password="temp-pass-123"
    )
    other_technician = await _create_technician(
        client, owner_token, email="tech-appt-b2@example.com", password="temp-pass-123"
    )
    tech_token = await _login(client, "tech-appt-b@example.com", "temp-pass-123")
    other_tech_token = await _login(client, "tech-appt-b2@example.com", "temp-pass-123")

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_token)
    )
    conversation_id = start_resp.json()["id"]
    fake_ai_provider.queue_reply(_book_appointment_reply())
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Can I book a tune-up?"},
        headers=_auth_headers(owner_token),
    )
    appointment = (
        await client.get("/api/v1/appointments", headers=_auth_headers(owner_token))
    ).json()[0]
    appointment_id = appointment["id"]

    schedule_payload = {
        "scheduled_start_at": "2026-06-01T15:00:00Z",
        "duration_minutes": 45,
        "technician_user_id": technician["user_id"],
    }

    # A Technician holds appointments:read + appointments:update_assigned,
    # not appointments:manage — scheduling is Owner/Admin-only.
    forbidden_schedule = await client.post(
        f"/api/v1/appointments/{appointment_id}/schedule",
        json=schedule_payload,
        headers=_auth_headers(tech_token),
    )
    assert forbidden_schedule.status_code == 403

    schedule_resp = await client.post(
        f"/api/v1/appointments/{appointment_id}/schedule",
        json=schedule_payload,
        headers=_auth_headers(owner_token),
    )
    assert schedule_resp.status_code == 200
    assert schedule_resp.json()["status"] == "scheduled"
    assert schedule_resp.json()["assigned_technician_user_id"] == technician["user_id"]

    # A technician the appointment is not assigned to cannot update its status.
    forbidden_status = await client.post(
        f"/api/v1/appointments/{appointment_id}/status",
        json={"status": "completed"},
        headers=_auth_headers(other_tech_token),
    )
    assert forbidden_status.status_code == 403

    completed_resp = await client.post(
        f"/api/v1/appointments/{appointment_id}/status",
        json={"status": "completed"},
        headers=_auth_headers(tech_token),
    )
    assert completed_resp.status_code == 200
    assert completed_resp.json()["status"] == "completed"
    assert completed_resp.json()["closed_at"] is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_schedule_rejects_time_outside_business_hours(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(
        client, "Appointment Flow Org C", "owner-appt-c@example.com"
    )

    # Close every day of the week — any scheduling attempt must be rejected.
    hours_resp = await client.put(
        "/api/v1/business-knowledge/hours",
        json={"entries": [{"day_of_week": d, "is_closed": True} for d in range(7)]},
        headers=_auth_headers(owner_token),
    )
    assert hours_resp.status_code == 200

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_token)
    )
    conversation_id = start_resp.json()["id"]
    fake_ai_provider.queue_reply(_book_appointment_reply())
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Can I book a tune-up?"},
        headers=_auth_headers(owner_token),
    )
    appointment_id = (
        await client.get("/api/v1/appointments", headers=_auth_headers(owner_token))
    ).json()[0]["id"]

    schedule_resp = await client.post(
        f"/api/v1/appointments/{appointment_id}/schedule",
        json={"scheduled_start_at": "2026-06-01T15:00:00Z", "duration_minutes": 30},
        headers=_auth_headers(owner_token),
    )
    assert schedule_resp.status_code == 400

    unaffected = await client.get(
        f"/api/v1/appointments/{appointment_id}", headers=_auth_headers(owner_token)
    )
    assert unaffected.json()["status"] == "requested"


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation_across_appointments(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_a, _ = await _register(client, "Appointment Flow Org D", "owner-appt-d@example.com")
    owner_b, _ = await _register(client, "Appointment Flow Org E", "owner-appt-e@example.com")

    start_resp = await client.post(
        "/api/v1/ai/conversations", json={}, headers=_auth_headers(owner_a)
    )
    conversation_id = start_resp.json()["id"]
    fake_ai_provider.queue_reply(_book_appointment_reply())
    await client.post(
        f"/api/v1/ai/conversations/{conversation_id}/messages",
        json={"message": "Can I book a tune-up?"},
        headers=_auth_headers(owner_a),
    )

    org_a_appointments = await client.get(
        "/api/v1/appointments", headers=_auth_headers(owner_a)
    )
    org_b_appointments = await client.get(
        "/api/v1/appointments", headers=_auth_headers(owner_b)
    )
    assert len(org_a_appointments.json()) == 1
    assert org_b_appointments.json() == []

    appointment_id = org_a_appointments.json()[0]["id"]
    cross_tenant_get = await client.get(
        f"/api/v1/appointments/{appointment_id}", headers=_auth_headers(owner_b)
    )
    assert cross_tenant_get.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_book_appointment_via_vapi_webhook_creates_appointment(
    client: AsyncClient, fake_ai_provider: FakeAIProvider
):
    owner_token, org_id = await _register(
        client, "Appointment Flow Org F", "owner-appt-f@example.com"
    )
    assistant_id = "asst_appointment_flow_f"
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=None,
                phone_number="+15005550006",
                is_active=True,
            )
        )
        await session.commit()

    fake_ai_provider.queue_reply(_book_appointment_reply())
    response = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {
                "id": "call_appt_f1",
                "assistantId": assistant_id,
                "customer": {"number": "+15551234567"},
            },
            "messages": [{"role": "user", "content": "Can I book a tune-up?"}],
        },
        headers=_vapi_headers(),
    )
    assert response.status_code == 200

    appointments_resp = await client.get(
        "/api/v1/appointments", headers=_auth_headers(owner_token)
    )
    assert appointments_resp.status_code == 200
    appointments = appointments_resp.json()
    assert len(appointments) == 1
    assert appointments[0]["status"] == "requested"
    assert appointments[0]["customer_name"] == "Jane Doe"
