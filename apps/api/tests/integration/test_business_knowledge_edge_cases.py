"""Edge-case coverage for Business Knowledge: duplicate-constraint handling,
validation boundaries, RBAC across every sub-resource, tenant isolation
across every sub-resource, and unauthenticated/malformed-token access.

Shares the same database_ready/client fixture pattern as
test_business_knowledge_flow.py (see that file's docstring for why
loop_scope="session" is required once more than one integration module
touches the shared async engine).
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


async def _member_token(org_id: uuid.UUID, email: str) -> str:
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RoleModel).where(RoleModel.organization_id == org_id, RoleModel.name == "Member")
        )
        member_role = result.scalar_one()
        user_repo = SqlAlchemyUserRepository(session)
        member_user = await user_repo.create(
            organization_id=org_id,
            email=email,
            hashed_password=hash_password("super-secret-123"),
            full_name="Member User",
            role_ids=[member_role.id],
        )
        await session.commit()
    return create_access_token(
        user_id=member_user.id,
        organization_id=member_user.organization_id,
        permissions=member_user.permission_codes,
        is_superuser=False,
        settings=settings,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


BASE = "/api/v1/business-knowledge"


# --- Duplicate-constraint handling: should be a clean 409, not a 500 ---


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_service_name_is_conflict_not_crash(client: AsyncClient):
    token, _ = await _register(client, "Dup Service Org", "dup-service@example.com")
    headers = _auth(token)
    payload = {
        "name": "Furnace Repair",
        "description": None,
        "category": None,
        "is_emergency_eligible": False,
        "is_active": True,
    }
    first = await client.post(f"{BASE}/services", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post(f"{BASE}/services", json=payload, headers=headers)
    assert second.status_code == 409, f"expected 409, got {second.status_code}: {second.text}"
    assert second.json()["error"]["code"] == "ENTITY_ALREADY_EXISTS"


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_emergency_keyword_is_conflict_not_crash(client: AsyncClient):
    token, _ = await _register(client, "Dup Keyword Org", "dup-keyword@example.com")
    headers = _auth(token)
    payload = {"phrase": "no heat", "notes": None}

    first = await client.post(f"{BASE}/emergency-keywords", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post(f"{BASE}/emergency-keywords", json=payload, headers=headers)
    assert second.status_code == 409, f"expected 409, got {second.status_code}: {second.text}"


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_hours_exception_date_is_conflict_not_crash(client: AsyncClient):
    token, _ = await _register(client, "Dup Exception Org", "dup-exception@example.com")
    headers = _auth(token)
    payload = {
        "date": "2026-12-25",
        "is_closed": True,
        "open_time": None,
        "close_time": None,
        "label": "Christmas",
    }
    first = await client.post(f"{BASE}/hours/exceptions", json=payload, headers=headers)
    assert first.status_code == 201

    second = await client.post(f"{BASE}/hours/exceptions", json=payload, headers=headers)
    assert second.status_code == 409, f"expected 409, got {second.status_code}: {second.text}"


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_faq_question_is_allowed(client: AsyncClient):
    token, _ = await _register(client, "Dup FAQ Org", "dup-faq@example.com")
    headers = _auth(token)
    payload = {
        "question": "Do you offer emergency service?",
        "answer": "Yes.",
        "category": None,
        "is_active": True,
    }
    first = await client.post(f"{BASE}/faqs", json=payload, headers=headers)
    second = await client.post(f"{BASE}/faqs", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201


# --- Validation boundaries ---


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_timezone_rejected_by_api(client: AsyncClient):
    token, _ = await _register(client, "Bad TZ Org", "bad-tz@example.com")
    headers = _auth(token)
    payload = {
        "business_type": "hvac",
        "display_name": "Acme",
        "phone_number": None,
        "timezone": "Not/AZone",
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "postal_code": None,
        "country": "US",
        "website": None,
    }
    response = await client.put(f"{BASE}/profile", json=payload, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_service_area_without_city_or_postal_code_rejected(client: AsyncClient):
    token, _ = await _register(client, "Bad Area Org", "bad-area@example.com")
    headers = _auth(token)
    response = await client.post(
        f"{BASE}/service-areas",
        json={"label": "Nowhere", "postal_code": None, "city": None, "state": None},
        headers=headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_hours_missing_a_day_rejected(client: AsyncClient):
    token, _ = await _register(client, "Short Week Org", "short-week@example.com")
    headers = _auth(token)
    entries = [
        {"day_of_week": day, "is_closed": True, "open_time": None, "close_time": None}
        for day in range(6)  # only 6 days, missing Sunday
    ]
    response = await client.put(f"{BASE}/hours", json={"entries": entries}, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_hours_duplicate_day_rejected(client: AsyncClient):
    token, _ = await _register(client, "Dup Day Org", "dup-day@example.com")
    headers = _auth(token)
    entries = [
        {"day_of_week": 0 if day == 6 else day, "is_closed": True, "open_time": None, "close_time": None}
        for day in range(7)
    ]
    response = await client.put(f"{BASE}/hours", json={"entries": entries}, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_hours_open_after_close_rejected(client: AsyncClient):
    token, _ = await _register(client, "Backwards Hours Org", "backwards-hours@example.com")
    headers = _auth(token)
    entries = [
        {
            "day_of_week": day,
            "is_closed": False,
            "open_time": "18:00",
            "close_time": "09:00",
        }
        for day in range(7)
    ]
    response = await client.put(f"{BASE}/hours", json={"entries": entries}, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_display_name_over_max_length_rejected(client: AsyncClient):
    token, _ = await _register(client, "Long Name Org", "long-name@example.com")
    headers = _auth(token)
    payload = {
        "business_type": "hvac",
        "display_name": "A" * 256,
        "phone_number": None,
        "timezone": "America/Chicago",
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "postal_code": None,
        "country": "US",
        "website": None,
    }
    response = await client.put(f"{BASE}/profile", json=payload, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_keyword_phrase_below_min_length_rejected(client: AsyncClient):
    token, _ = await _register(client, "Short Phrase Org", "short-phrase@example.com")
    headers = _auth(token)
    response = await client.post(
        f"{BASE}/emergency-keywords", json={"phrase": "x", "notes": None}, headers=headers
    )
    assert response.status_code == 422


# --- Unauthenticated / malformed token access ---


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "path",
    [
        "/profile",
        "/hours",
        "/hours/exceptions",
        "/service-areas",
        "/services",
        "/emergency-keywords",
        "/faqs",
    ],
)
async def test_unauthenticated_requests_are_rejected(client: AsyncClient, path: str):
    response = await client.get(f"{BASE}{path}")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_malformed_bearer_token_is_rejected(client: AsyncClient):
    response = await client.get(f"{BASE}/services", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


# --- RBAC: Member role is read-only across every sub-resource ---


@pytest.mark.asyncio(loop_scope="session")
async def test_member_can_read_every_resource(client: AsyncClient):
    _, org_id = await _register(client, "RBAC Read Org", "rbac-read@example.com")
    member_headers = _auth(await _member_token(org_id, "rbac-read-member@example.com"))

    for path in [
        "/hours",
        "/hours/exceptions",
        "/service-areas",
        "/services",
        "/emergency-keywords",
        "/faqs",
    ]:
        response = await client.get(f"{BASE}{path}", headers=member_headers)
        assert response.status_code == 200, f"{path} failed: {response.text}"

    # /profile is a special case: 404 until configured, but that still means
    # the read permission check passed (403 would indicate a permission bug).
    profile_response = await client.get(f"{BASE}/profile", headers=member_headers)
    assert profile_response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_member_cannot_write_any_resource(client: AsyncClient):
    _, org_id = await _register(client, "RBAC Write Org", "rbac-write@example.com")
    member_headers = _auth(await _member_token(org_id, "rbac-write-member@example.com"))

    attempts = [
        ("PUT", "/profile", {
            "business_type": "hvac", "display_name": "X", "phone_number": None,
            "timezone": "UTC", "address_line1": None, "address_line2": None,
            "city": None, "state": None, "postal_code": None, "country": "US", "website": None,
        }),
        ("POST", "/service-areas", {"label": "X", "postal_code": "00000", "city": None, "state": None}),
        ("POST", "/services", {
            "name": "X", "description": None, "category": None,
            "is_emergency_eligible": False, "is_active": True,
        }),
        ("POST", "/emergency-keywords", {"phrase": "gas leak", "notes": None}),
        ("POST", "/faqs", {"question": "X?", "answer": "Y.", "category": None, "is_active": True}),
    ]
    for method, path, body in attempts:
        response = await client.request(method, f"{BASE}{path}", json=body, headers=member_headers)
        assert response.status_code == 403, f"{method} {path} expected 403, got {response.status_code}"


# --- Tenant isolation across every mutable sub-resource ---


@pytest.mark.asyncio(loop_scope="session")
async def test_tenant_isolation_across_all_resources(client: AsyncClient):
    token_a, _ = await _register(client, "Isolation Org A", "isolation-a@example.com")
    token_b, _ = await _register(client, "Isolation Org B", "isolation-b@example.com")
    headers_a, headers_b = _auth(token_a), _auth(token_b)

    area = await client.post(
        f"{BASE}/service-areas",
        json={"label": "X", "postal_code": "00000", "city": None, "state": None},
        headers=headers_a,
    )
    keyword = await client.post(
        f"{BASE}/emergency-keywords", json={"phrase": "gas leak", "notes": None}, headers=headers_a
    )
    faq = await client.post(
        f"{BASE}/faqs",
        json={"question": "X?", "answer": "Y.", "category": None, "is_active": True},
        headers=headers_a,
    )
    exception = await client.post(
        f"{BASE}/hours/exceptions",
        json={"date": "2026-11-26", "is_closed": True, "open_time": None, "close_time": None, "label": None},
        headers=headers_a,
    )

    deletions = [
        (f"/service-areas/{area.json()['id']}"),
        (f"/emergency-keywords/{keyword.json()['id']}"),
        (f"/faqs/{faq.json()['id']}"),
        (f"/hours/exceptions/{exception.json()['id']}"),
    ]
    for path in deletions:
        response = await client.delete(f"{BASE}{path}", headers=headers_b)
        assert response.status_code == 404, f"{path} expected 404 for cross-org delete, got {response.status_code}"

    # Org A can still delete its own records afterward.
    for path in deletions:
        response = await client.delete(f"{BASE}{path}", headers=headers_a)
        assert response.status_code == 204
