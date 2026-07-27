"""End-to-end Organization settings flow (Milestone 9) against a real
Postgres database — fixtures mirror `test_auth_flow.py`'s exactly."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.session import Base, engine
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


async def _register(client: AsyncClient, org_name: str, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Owner Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201
    return response.json()["tokens"]["access_token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio(loop_scope="session")
async def test_rename_organization(client: AsyncClient):
    owner_token = await _register(client, "Org Flow HVAC", "org-owner@example.com")

    get_response = await client.get(
        "/api/v1/organizations/current", headers=_auth_headers(owner_token)
    )
    assert get_response.status_code == 200
    assert get_response.json()["name"] == "Org Flow HVAC"
    assert get_response.json()["is_active"] is True

    rename_response = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth_headers(owner_token),
        json={"name": "Org Flow Home Services"},
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["name"] == "Org Flow Home Services"
    assert rename_response.json()["is_active"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_member_cannot_manage_organization(client: AsyncClient):
    owner_token = await _register(client, "Member RBAC HVAC", "member-rbac-owner@example.com")
    invite_response = await client.post(
        "/api/v1/team/members",
        headers=_auth_headers(owner_token),
        json={
            "full_name": "Mia Member",
            "email": "member-rbac-member@example.com",
            "temporary_password": "temp-pass-123",
            "role": "Member",
        },
    )
    assert invite_response.status_code == 201
    member_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "member-rbac-member@example.com", "password": "temp-pass-123"},
    )
    member_token = member_login.json()["tokens"]["access_token"]

    response = await client.get(
        "/api/v1/organizations/current", headers=_auth_headers(member_token)
    )
    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivating_organization_blocks_future_logins(client: AsyncClient):
    owner_token = await _register(client, "Deactivate Org HVAC", "deactivate-owner@example.com")

    deactivate_response = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth_headers(owner_token),
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["is_active"] is False

    blocked_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "deactivate-owner@example.com", "password": "super-secret-123"},
    )
    assert blocked_login.status_code == 403

    blocked_refresh = await client.post("/api/v1/auth/refresh")
    assert blocked_refresh.status_code == 403
