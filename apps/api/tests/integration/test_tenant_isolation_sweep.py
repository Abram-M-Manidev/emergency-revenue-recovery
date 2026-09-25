"""Tenant isolation, swept across every ID-bearing route in one place.

Per-module flows already assert isolation for the resources they own. This
module exists because a release gate should not depend on every module
having remembered to: it builds two fully-populated tenants through the same
paths production uses (the Vapi webhook with real tools, the dashboard API)
and then has Tenant A present each of Tenant B's identifiers to every route
that accepts one — read, write, and assignment — and finally proves B's
records are byte-for-byte what they were.

Three cases the module flows did not cover are the reason it was written:
team membership (role and status changes), voice-call metadata, and
cross-tenant *references* — assigning your own ticket or appointment to
another tenant's technician.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.appointment import AppointmentModel
from app.infrastructure.database.models.customer import CustomerModel
from app.infrastructure.database.models.emergency_ticket import EmergencyTicketModel
from app.infrastructure.database.models.user import UserModel
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-tenant-sweep"


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        from app.domain.exceptions import InvalidTokenError

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
async def provider():
    scripted = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    yield scripted
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready, provider):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@dataclass
class Tenant:
    token: str
    org_id: uuid.UUID
    assistant_id: str
    ticket_id: str = ""
    ticket_conversation_id: str = ""
    appointment_id: str = ""
    customer_id: str = ""
    technician_id: str = ""
    member_id: str = ""
    service_id: str = ""
    faq_id: str = ""
    keyword_id: str = ""
    area_id: str = ""
    exception_id: str = ""
    call_id: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _build_tenant(client: AsyncClient, provider: ScriptedToolAIProvider, label: str) -> Tenant:
    suffix = uuid.uuid4().hex[:8]
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": f"{label} {suffix}",
            "full_name": "Owner",
            "email": f"sweep-{label.lower()}-{suffix}@example.com",
            "password": "super-secret-123",
        },
    )
    assert registered.status_code == 201, registered.text
    tenant = Tenant(
        token=registered.json()["tokens"]["access_token"],
        org_id=uuid.UUID(registered.json()["user"]["organization_id"]),
        assistant_id=f"asst_sweep_{suffix}",
    )
    h = tenant.headers

    assert (
        await client.put(
            "/api/v1/business-knowledge/profile",
            json={
                "business_type": "hvac",
                "display_name": f"{label} HVAC",
                "phone_number": None,
                "timezone": "UTC",
                "address_line1": None,
                "address_line2": None,
                "city": None,
                "state": None,
                "postal_code": None,
                "country": "US",
                "website": None,
            },
            headers=h,
        )
    ).status_code == 200
    assert (
        await client.put(
            "/api/v1/business-knowledge/hours",
            json={
                "entries": [
                    {"day_of_week": d, "is_closed": False, "open_time": "00:00:00", "close_time": "23:59:00"}
                    for d in range(7)
                ]
            },
            headers=h,
        )
    ).status_code == 200

    service = await client.post(
        "/api/v1/business-knowledge/services",
        json={
            "name": "AC Repair",
            "description": None,
            "category": None,
            "is_emergency_eligible": False,
            "is_active": True,
            "default_duration_minutes": 60,
            "default_price": None,
        },
        headers=h,
    )
    assert service.status_code in (200, 201), service.text
    tenant.service_id = service.json()["id"]
    faq = await client.post(
        "/api/v1/business-knowledge/faqs",
        json={"question": "Do you service boilers?", "answer": "Yes.", "category": None},
        headers=h,
    )
    assert faq.status_code == 201, faq.text
    tenant.faq_id = faq.json()["id"]
    keyword = await client.post(
        "/api/v1/business-knowledge/emergency-keywords",
        json={"phrase": "gas smell", "notes": None},
        headers=h,
    )
    assert keyword.status_code == 201, keyword.text
    tenant.keyword_id = keyword.json()["id"]
    area = await client.post(
        "/api/v1/business-knowledge/service-areas",
        json={"label": "Lisle", "postal_code": "60532", "city": "Lisle", "state": "IL"},
        headers=h,
    )
    assert area.status_code == 201, area.text
    tenant.area_id = area.json()["id"]
    exception = await client.post(
        "/api/v1/business-knowledge/hours/exceptions",
        json={
            "date": (datetime.now(timezone.utc).date() + timedelta(days=60)).isoformat(),
            "is_closed": True,
            "open_time": None,
            "close_time": None,
            "label": "Holiday",
        },
        headers=h,
    )
    assert exception.status_code == 201, exception.text
    tenant.exception_id = exception.json()["id"]

    technician = await client.post(
        "/api/v1/dispatch/technicians",
        json={
            "full_name": f"{label} Tech",
            "email": f"sweep-tech-{label.lower()}-{suffix}@example.com",
            "phone_number": "6305550100",
            "temporary_password": "temp-pass-123",
        },
        headers=h,
    )
    assert technician.status_code == 201, technician.text
    tenant.technician_id = technician.json()["user_id"] if "user_id" in technician.json() else technician.json()["id"]

    member = await client.post(
        "/api/v1/team/members",
        json={
            "full_name": f"{label} Member",
            "email": f"sweep-member-{label.lower()}-{suffix}@example.com",
            "temporary_password": "temp-pass-123",
            "role": "Member",
        },
        headers=h,
    )
    assert member.status_code == 201, member.text
    tenant.member_id = member.json()["id"]

    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=tenant.org_id,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=tenant.assistant_id,
                vapi_phone_number_id=None,
                phone_number=None,
                is_active=True,
            )
        )
        await session.commit()

    # An emergency call -> ticket + customer, through the real tool path.
    tenant.call_id = f"call_sweep_emerg_{suffix}"
    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    "customer_name": f"{label} Caller",
                    "customer_phone": "6305550184" if label == "A" else "6305550199",
                    "service_address": f"1 {label} Street",
                    "problem_description": "Gas smell in the basement.",
                    "classification": "emergency",
                    "service_name": None,
                },
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer=f"{label} secret reply."))
    call = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {"id": tenant.call_id, "assistantId": tenant.assistant_id},
            "messages": [{"role": "user", "content": f"{label} utterance"}],
        },
        headers={"x-vapi-secret": _SECRET},
    )
    assert call.status_code == 200, call.text

    # A standard call -> appointment.
    provider.queue_tool_round(
        [
            (
                "create_service_request",
                {
                    "customer_name": f"{label} Booker",
                    "customer_phone": "6305550111" if label == "A" else "6305550122",
                    "service_address": f"2 {label} Street",
                    "problem_description": "AC not cooling.",
                    "classification": "non_emergency",
                    "service_name": "AC Repair",
                },
            )
        ]
    )
    provider.queue_reply(default_reply(message_to_customer="Recorded."))
    call = await client.post(
        "/api/v1/voice/vapi/chat/completions",
        json={
            "call": {"id": f"call_sweep_appt_{suffix}", "assistantId": tenant.assistant_id},
            "messages": [{"role": "user", "content": "AC broken"}],
        },
        headers={"x-vapi-secret": _SECRET},
    )
    assert call.status_code == 200, call.text

    tickets = (await client.get("/api/v1/dispatch/tickets", headers=h)).json()
    assert len(tickets) == 1
    tenant.ticket_id = tickets[0]["id"]
    tenant.ticket_conversation_id = tickets[0]["conversation_id"]
    appointments = (await client.get("/api/v1/appointments", headers=h)).json()
    assert len(appointments) == 1
    tenant.appointment_id = appointments[0]["id"]
    customers = (await client.get("/api/v1/customers", headers=h)).json()
    assert customers
    tenant.customer_id = customers[0]["id"]
    return tenant


async def _snapshot(org_id: uuid.UUID) -> dict:
    """Everything of B's an attack could have altered, as plain values."""
    async with AsyncSessionLocal() as session:
        tickets = (
            await session.execute(
                select(EmergencyTicketModel).where(EmergencyTicketModel.organization_id == org_id)
            )
        ).scalars().all()
        appointments = (
            await session.execute(
                select(AppointmentModel).where(AppointmentModel.organization_id == org_id)
            )
        ).scalars().all()
        customers = (
            await session.execute(select(CustomerModel).where(CustomerModel.organization_id == org_id))
        ).scalars().all()
        users = (
            await session.execute(select(UserModel).where(UserModel.organization_id == org_id))
        ).scalars().all()
        return {
            "tickets": sorted(
                (str(t.id), t.status.value, str(t.assigned_technician_user_id)) for t in tickets
            ),
            "appointments": sorted(
                (str(a.id), a.status.value, str(a.scheduled_start_at), str(a.assigned_technician_user_id))
                for a in appointments
            ),
            "customers": sorted((str(c.id), c.full_name, c.phone_number, c.notes) for c in customers),
            "users": sorted((str(u.id), u.is_active) for u in users),
        }


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_a_cannot_reach_any_of_tenant_bs_records(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    a = await _build_tenant(client, provider, "A")
    b = await _build_tenant(client, provider, "B")
    before = await _snapshot(b.org_id)
    h = a.headers
    future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(
        hour=15, minute=0, second=0, microsecond=0
    )

    # Every route that takes an identifier, presented with B's.
    attacks = [
        ("GET", f"/api/v1/dispatch/tickets/{b.ticket_id}", None),
        ("POST", f"/api/v1/dispatch/tickets/{b.ticket_id}/assign", {"technician_user_id": a.technician_id}),
        ("POST", f"/api/v1/dispatch/tickets/{b.ticket_id}/status", {"status": "canceled"}),
        ("PATCH", f"/api/v1/dispatch/technicians/{b.technician_id}/on-call", {"is_on_call": False}),
        ("GET", f"/api/v1/appointments/{b.appointment_id}", None),
        (
            "POST",
            f"/api/v1/appointments/{b.appointment_id}/schedule",
            {"scheduled_start_at": future.isoformat(), "duration_minutes": 60},
        ),
        ("POST", f"/api/v1/appointments/{b.appointment_id}/status", {"status": "canceled"}),
        ("GET", f"/api/v1/customers/{b.customer_id}", None),
        (
            "PATCH",
            f"/api/v1/customers/{b.customer_id}",
            {"full_name": "Hijacked", "phone_number": "1", "email": None, "address": None, "notes": "x"},
        ),
        ("GET", f"/api/v1/ai/conversations/{b.ticket_conversation_id}", None),
        ("POST", f"/api/v1/ai/conversations/{b.ticket_conversation_id}/messages", {"message": "hi"}),
        ("GET", f"/api/v1/voice/calls/{b.ticket_conversation_id}", None),
        ("PATCH", f"/api/v1/team/members/{b.member_id}/status", {"is_active": False}),
        ("PATCH", f"/api/v1/team/members/{b.member_id}/role", {"role": "Admin"}),
        ("PATCH", f"/api/v1/business-knowledge/services/{b.service_id}", {"name": "Hijacked"}),
        ("DELETE", f"/api/v1/business-knowledge/services/{b.service_id}", None),
        ("PATCH", f"/api/v1/business-knowledge/faqs/{b.faq_id}", {"question": "x", "answer": "y", "category": None}),
        ("DELETE", f"/api/v1/business-knowledge/faqs/{b.faq_id}", None),
        ("DELETE", f"/api/v1/business-knowledge/emergency-keywords/{b.keyword_id}", None),
        ("DELETE", f"/api/v1/business-knowledge/service-areas/{b.area_id}", None),
        ("DELETE", f"/api/v1/business-knowledge/hours/exceptions/{b.exception_id}", None),
        # Cross-tenant REFERENCES: A's own records pointed at B's technician.
        ("POST", f"/api/v1/dispatch/tickets/{a.ticket_id}/assign", {"technician_user_id": b.technician_id}),
        (
            "POST",
            f"/api/v1/appointments/{a.appointment_id}/schedule",
            {
                "scheduled_start_at": future.isoformat(),
                "duration_minutes": 60,
                "technician_user_id": b.technician_id,
            },
        ),
    ]
    leaks = []
    for method, path, body in attacks:
        response = await client.request(method, path, json=body, headers=h)
        # Exactly 404 — "does not exist", from A's point of view. Not merely
        # "some 4xx": a 422 would mean the body was rejected before the
        # ownership check ran, and the attack would prove nothing.
        if response.status_code != 404:
            leaks.append((method, path, response.status_code, response.text[:200]))
        assert "B Caller" not in response.text and "B secret reply" not in response.text
    assert leaks == [], leaks

    # B's knowledge base is intact from B's own point of view.
    services = (await client.get("/api/v1/business-knowledge/services", headers=b.headers)).json()
    assert [s["name"] for s in services] == ["AC Repair"]
    assert len((await client.get("/api/v1/business-knowledge/faqs", headers=b.headers)).json()) == 1
    assert len((await client.get("/api/v1/business-knowledge/emergency-keywords", headers=b.headers)).json()) == 1
    assert len((await client.get("/api/v1/business-knowledge/service-areas", headers=b.headers)).json()) == 1
    assert len((await client.get("/api/v1/business-knowledge/hours/exceptions", headers=b.headers)).json()) == 1

    # Listing routes never include the other tenant.
    for path in ("/api/v1/dispatch/tickets", "/api/v1/appointments", "/api/v1/customers",
                 "/api/v1/ai/conversations", "/api/v1/team/members", "/api/v1/dispatch/technicians"):
        body = (await client.get(path, headers=h)).text
        assert b.ticket_id not in body and b.customer_id not in body and b.member_id not in body
        assert "B Caller" not in body and "B Booker" not in body

    assert await _snapshot(b.org_id) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_a_voice_call_id_from_another_tenant_never_continues_that_conversation(
    client: AsyncClient, provider: ScriptedToolAIProvider
):
    """Vapi call ids are global. If A's assistant ever presented a call id
    already bound to B's conversation, the turn must not read, answer from,
    or write to B's conversation — including B's cached last reply."""
    a = await _build_tenant(client, provider, "A")
    b = await _build_tenant(client, provider, "B")
    async with AsyncSessionLocal() as session:
        b_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == b.call_id))
        ).scalar_one()
        b_messages_before = len(
            (await session.execute(text(
                "SELECT id FROM conversation_messages WHERE conversation_id = :c"
            ), {"c": b_call.conversation_id})).all()
        )

    for stream in (False, True):
        response = await client.post(
            "/api/v1/voice/vapi/chat/completions",
            json={
                "call": {"id": b.call_id, "assistantId": a.assistant_id},
                # B's exact last utterance: the cached-reply path.
                "messages": [{"role": "user", "content": "B utterance"}],
                "stream": stream,
            },
            headers={"x-vapi-secret": _SECRET},
        )
        assert response.status_code == 200
        assert "B secret reply" not in response.text

    async with AsyncSessionLocal() as session:
        b_messages_after = len(
            (await session.execute(text(
                "SELECT id FROM conversation_messages WHERE conversation_id = :c"
            ), {"c": b_call.conversation_id})).all()
        )
    assert b_messages_after == b_messages_before
