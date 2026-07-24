"""End-to-end auth flow against a real Postgres database.

Requires DATABASE_URL (see tests/conftest.py) to point at a reachable,
disposable database — e.g. the `postgres` service from docker-compose.yml,
pointed at a `_test` database. Skips gracefully if no database is reachable
so `pytest` still runs unit tests in environments without Docker.
"""

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


@pytest.mark.asyncio(loop_scope="session")
async def test_register_login_me_refresh_logout_flow(client: AsyncClient):
    register_payload = {
        "organization_name": "Acme HVAC",
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "password": "super-secret-123",
    }
    register_response = await client.post("/api/v1/auth/register", json=register_payload)
    assert register_response.status_code == 201
    body = register_response.json()
    assert body["user"]["email"] == register_payload["email"]
    assert "Owner" in body["user"]["roles"]
    access_token = body["tokens"]["access_token"]
    assert "refresh_token" in register_response.cookies

    me_response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == register_payload["email"]

    unauthorized_response = await client.get("/api/v1/auth/me")
    assert unauthorized_response.status_code == 401

    login_response = await client.post(
        "/api/v1/auth/login",
        json={"email": register_payload["email"], "password": register_payload["password"]},
    )
    assert login_response.status_code == 200

    refresh_response = await client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 200
    assert refresh_response.json()["tokens"]["access_token"]

    logout_response = await client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204

    stale_refresh_response = await client.post("/api/v1/auth/refresh")
    assert stale_refresh_response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_register_rejects_duplicate_email(client: AsyncClient):
    payload = {
        "organization_name": "Beta Plumbing",
        "full_name": "Grace Hopper",
        "email": "grace@example.com",
        "password": "super-secret-123",
    }
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ENTITY_ALREADY_EXISTS"
