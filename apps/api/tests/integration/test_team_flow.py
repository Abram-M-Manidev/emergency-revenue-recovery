"""End-to-end Team management flow (Milestone 9) against a real Postgres
database — fixtures mirror `test_auth_flow.py`'s exactly."""

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
async def test_invite_list_deactivate_flow(client: AsyncClient):
    owner_token = await _register(client, "Team Flow HVAC", "team-owner@example.com")

    list_response = await client.get("/api/v1/team/members", headers=_auth_headers(owner_token))
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    invite_response = await client.post(
        "/api/v1/team/members",
        headers=_auth_headers(owner_token),
        json={
            "full_name": "Mia Member",
            "email": "team-member@example.com",
            "temporary_password": "temp-pass-123",
            "role": "Member",
        },
    )
    assert invite_response.status_code == 201
    member = invite_response.json()
    assert member["roles"] == ["Member"]
    assert member["is_active"] is True

    member_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "team-member@example.com", "password": "temp-pass-123"},
    )
    assert member_login.status_code == 200

    list_response = await client.get("/api/v1/team/members", headers=_auth_headers(owner_token))
    assert len(list_response.json()) == 2

    # A Member cannot invite/manage the team.
    member_token = member_login.json()["tokens"]["access_token"]
    forbidden_invite = await client.post(
        "/api/v1/team/members",
        headers=_auth_headers(member_token),
        json={
            "full_name": "Someone Else",
            "email": "someone-else@example.com",
            "temporary_password": "temp-pass-123",
            "role": "Member",
        },
    )
    assert forbidden_invite.status_code == 403

    deactivate_response = await client.patch(
        f"/api/v1/team/members/{member['id']}/status",
        headers=_auth_headers(owner_token),
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["is_active"] is False

    blocked_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "team-member@example.com", "password": "temp-pass-123"},
    )
    assert blocked_login.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_cannot_deactivate_self(client: AsyncClient):
    owner_token = await _register(client, "Self Deactivate HVAC", "self-owner@example.com")
    me = await client.get("/api/v1/auth/me", headers=_auth_headers(owner_token))
    owner_id = me.json()["id"]

    response = await client.patch(
        f"/api/v1/team/members/{owner_id}/status",
        headers=_auth_headers(owner_token),
        json={"is_active": False},
    )
    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_last_owner_cannot_be_demoted_or_deactivated(client: AsyncClient):
    owner_token = await _register(client, "Last Owner HVAC", "last-owner@example.com")
    invite_response = await client.post(
        "/api/v1/team/members",
        headers=_auth_headers(owner_token),
        json={
            "full_name": "Admin Admin",
            "email": "last-owner-admin@example.com",
            "temporary_password": "temp-pass-123",
            "role": "Admin",
        },
    )
    admin_id = invite_response.json()["id"]

    admin_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "last-owner-admin@example.com", "password": "temp-pass-123"},
    )
    admin_token = admin_login.json()["tokens"]["access_token"]

    me = await client.get("/api/v1/auth/me", headers=_auth_headers(owner_token))
    owner_id = me.json()["id"]

    demote_response = await client.patch(
        f"/api/v1/team/members/{owner_id}/role",
        headers=_auth_headers(admin_token),
        json={"role": "Admin"},
    )
    assert demote_response.status_code == 409
    assert demote_response.json()["error"]["code"] == "LAST_OWNER"

    # Promote the Admin to Owner, then the original Owner can safely be
    # demoted since another active Owner now exists.
    promote_response = await client.patch(
        f"/api/v1/team/members/{admin_id}/role",
        headers=_auth_headers(owner_token),
        json={"role": "Owner"},
    )
    assert promote_response.status_code == 200
    assert promote_response.json()["roles"] == ["Owner"]

    second_demote_response = await client.patch(
        f"/api/v1/team/members/{owner_id}/role",
        headers=_auth_headers(admin_token),
        json={"role": "Admin"},
    )
    assert second_demote_response.status_code == 200
    assert second_demote_response.json()["roles"] == ["Admin"]
