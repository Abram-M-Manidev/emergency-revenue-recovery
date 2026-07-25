"""End-to-end Business Knowledge flow against a real Postgres database.

Requires DATABASE_URL (see tests/conftest.py) to point at a reachable,
disposable database. Skips gracefully if no database is reachable, same as
test_auth_flow.py.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.role import RoleModel
from app.infrastructure.database.repositories import SqlAlchemyUserRepository
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.infrastructure.security.jwt import create_access_token
from app.infrastructure.security.password import hash_password
from app.main import app


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
async def client(database_ready):
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
async def test_business_knowledge_full_crud_flow(client: AsyncClient):
    token, _ = await _register(client, "Acme HVAC", "owner-a@example.com")
    headers = _auth_headers(token)

    # Profile isn't configured yet.
    profile_resp = await client.get("/api/v1/business-knowledge/profile", headers=headers)
    assert profile_resp.status_code == 404

    profile_payload = {
        "business_type": "hvac",
        "display_name": "Acme HVAC Services",
        "phone_number": "+15551234567",
        "timezone": "America/Chicago",
        "address_line1": "123 Main St",
        "address_line2": None,
        "city": "Springfield",
        "state": "IL",
        "postal_code": "62701",
        "country": "US",
        "website": "https://acme-hvac.example.com",
    }
    put_profile = await client.put(
        "/api/v1/business-knowledge/profile", json=profile_payload, headers=headers
    )
    assert put_profile.status_code == 200
    assert put_profile.json()["display_name"] == "Acme HVAC Services"

    get_profile = await client.get("/api/v1/business-knowledge/profile", headers=headers)
    assert get_profile.status_code == 200
    assert get_profile.json()["timezone"] == "America/Chicago"

    # Weekly hours: empty until set.
    hours_resp = await client.get("/api/v1/business-knowledge/hours", headers=headers)
    assert hours_resp.status_code == 200
    assert hours_resp.json() == []

    weekly_payload = {
        "entries": [
            {
                "day_of_week": day,
                "is_closed": day == 6,
                "open_time": None if day == 6 else "08:00:00",
                "close_time": None if day == 6 else "17:00:00",
            }
            for day in range(7)
        ]
    }
    put_hours = await client.put(
        "/api/v1/business-knowledge/hours", json=weekly_payload, headers=headers
    )
    assert put_hours.status_code == 200
    assert len(put_hours.json()) == 7

    # Hours exceptions.
    exception_payload = {
        "date": "2026-12-25",
        "is_closed": True,
        "open_time": None,
        "close_time": None,
        "label": "Christmas Day",
    }
    create_exc = await client.post(
        "/api/v1/business-knowledge/hours/exceptions", json=exception_payload, headers=headers
    )
    assert create_exc.status_code == 201
    exception_id = create_exc.json()["id"]

    list_exc = await client.get("/api/v1/business-knowledge/hours/exceptions", headers=headers)
    assert len(list_exc.json()) == 1

    delete_exc = await client.delete(
        f"/api/v1/business-knowledge/hours/exceptions/{exception_id}", headers=headers
    )
    assert delete_exc.status_code == 204

    # Service areas.
    area_payload = {"label": "Downtown", "postal_code": "62701", "city": None, "state": None}
    create_area = await client.post(
        "/api/v1/business-knowledge/service-areas", json=area_payload, headers=headers
    )
    assert create_area.status_code == 201
    area_id = create_area.json()["id"]

    delete_area = await client.delete(
        f"/api/v1/business-knowledge/service-areas/{area_id}", headers=headers
    )
    assert delete_area.status_code == 204

    # Services.
    service_payload = {
        "name": "Furnace Repair",
        "description": "Repair broken furnaces",
        "category": "Repair",
        "is_emergency_eligible": True,
        "is_active": True,
        "default_duration_minutes": 60,
    }
    create_service = await client.post(
        "/api/v1/business-knowledge/services", json=service_payload, headers=headers
    )
    assert create_service.status_code == 201
    assert create_service.json()["default_duration_minutes"] == 60
    service_id = create_service.json()["id"]

    update_service_payload = {**service_payload, "is_active": False}
    update_service = await client.patch(
        f"/api/v1/business-knowledge/services/{service_id}",
        json=update_service_payload,
        headers=headers,
    )
    assert update_service.status_code == 200
    assert update_service.json()["is_active"] is False

    # Emergency keywords.
    keyword_payload = {"phrase": "no heat", "notes": "Winter emergency"}
    create_keyword = await client.post(
        "/api/v1/business-knowledge/emergency-keywords", json=keyword_payload, headers=headers
    )
    assert create_keyword.status_code == 201
    keyword_id = create_keyword.json()["id"]

    # FAQs.
    faq_payload = {
        "question": "Do you offer emergency service?",
        "answer": "Yes, 24/7.",
        "category": "General",
        "is_active": True,
    }
    create_faq = await client.post(
        "/api/v1/business-knowledge/faqs", json=faq_payload, headers=headers
    )
    assert create_faq.status_code == 201
    faq_id = create_faq.json()["id"]

    update_faq_payload = {**faq_payload, "answer": "Yes, 24/7, every day."}
    update_faq = await client.patch(
        f"/api/v1/business-knowledge/faqs/{faq_id}", json=update_faq_payload, headers=headers
    )
    assert update_faq.status_code == 200
    assert update_faq.json()["answer"] == "Yes, 24/7, every day."

    # Cleanup mutations all succeed.
    assert (
        await client.delete(f"/api/v1/business-knowledge/faqs/{faq_id}", headers=headers)
    ).status_code == 204
    assert (
        await client.delete(
            f"/api/v1/business-knowledge/emergency-keywords/{keyword_id}", headers=headers
        )
    ).status_code == 204
    assert (
        await client.delete(
            f"/api/v1/business-knowledge/services/{service_id}", headers=headers
        )
    ).status_code == 204


@pytest.mark.asyncio(loop_scope="session")
async def test_cross_tenant_access_is_isolated(client: AsyncClient):
    token_a, _ = await _register(client, "Org Alpha", "owner-alpha@example.com")
    token_b, _ = await _register(client, "Org Bravo", "owner-bravo@example.com")

    headers_a = _auth_headers(token_a)
    headers_b = _auth_headers(token_b)

    service_payload = {
        "name": "AC Installation",
        "description": None,
        "category": None,
        "is_emergency_eligible": False,
        "is_active": True,
    }
    created = await client.post(
        "/api/v1/business-knowledge/services", json=service_payload, headers=headers_a
    )
    assert created.status_code == 201
    service_id = created.json()["id"]

    # Org B's list never contains Org A's service.
    list_b = await client.get("/api/v1/business-knowledge/services", headers=headers_b)
    assert list_b.status_code == 200
    assert all(service["id"] != service_id for service in list_b.json())

    # Org B cannot update or delete Org A's service by id — reads as not found.
    update_attempt = await client.patch(
        f"/api/v1/business-knowledge/services/{service_id}",
        json={**service_payload, "is_active": False},
        headers=headers_b,
    )
    assert update_attempt.status_code == 404

    delete_attempt = await client.delete(
        f"/api/v1/business-knowledge/services/{service_id}", headers=headers_b
    )
    assert delete_attempt.status_code == 404

    # Org A's data is untouched.
    list_a = await client.get("/api/v1/business-knowledge/services", headers=headers_a)
    assert any(service["id"] == service_id for service in list_a.json())


@pytest.mark.asyncio(loop_scope="session")
async def test_member_role_is_read_only(client: AsyncClient):
    _, org_id = await _register(client, "Org Charlie", "owner-charlie@example.com")
    settings = get_settings()

    # No invite-user endpoint exists yet (out of scope for this milestone) —
    # create the Member-role user directly through the repository/model
    # layer to exercise the read-only permission boundary in isolation.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RoleModel).where(
                RoleModel.organization_id == org_id, RoleModel.name == "Member"
            )
        )
        member_role = result.scalar_one()

        user_repo = SqlAlchemyUserRepository(session)
        member_user = await user_repo.create(
            organization_id=org_id,
            email="member-charlie@example.com",
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

    list_resp = await client.get("/api/v1/business-knowledge/services", headers=member_headers)
    assert list_resp.status_code == 200

    create_resp = await client.post(
        "/api/v1/business-knowledge/services",
        json={
            "name": "Should Fail",
            "description": None,
            "category": None,
            "is_emergency_eligible": False,
            "is_active": True,
        },
        headers=member_headers,
    )
    assert create_resp.status_code == 403
