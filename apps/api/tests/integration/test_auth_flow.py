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


# --- Registration gate, over the real request path ---------------------------
#
# `test_registration_gate.py` proves the rule at the service. These prove what
# a client actually sees: the status code, the machine-readable error code,
# and that a closed deployment still lets existing users in.
#
# `FEATURE_REGISTRATION_ENABLED` is read through `get_settings()`, which is
# `lru_cache`d, so the override goes through FastAPI's dependency system
# rather than by mutating the environment — that keeps the change scoped to
# the test and cannot leak into another module's settings.


@pytest_asyncio.fixture(loop_scope="session")
async def registration_disabled():
    from app.core.config import get_settings
    from app.main import fastapi_app

    base = get_settings()
    fastapi_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"FEATURE_REGISTRATION_ENABLED": False}
    )
    yield
    fastapi_app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_is_refused_when_the_flag_is_off(
    client: AsyncClient, registration_disabled
):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Closed Door HVAC",
            "full_name": "Nobody Here",
            "email": "closed-door@example.com",
            "password": "super-secret-123",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REGISTRATION_DISABLED"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_closed_deployment_does_not_reveal_whether_an_account_exists(
    client: AsyncClient,
):
    """The reason the flag is checked before the email lookup.

    Registers an address while the door is open, then shuts it and asks
    twice — once with that address, once with an unknown one. Both answers
    must be byte-identical, or the endpoint is an account oracle for anyone
    who can reach it."""
    known = "oracle-probe@example.com"
    opened = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Oracle Probe HVAC",
            "full_name": "Real User",
            "email": known,
            "password": "super-secret-123",
        },
    )
    assert opened.status_code == 201

    from app.core.config import get_settings
    from app.main import fastapi_app

    base = get_settings()
    fastapi_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"FEATURE_REGISTRATION_ENABLED": False}
    )
    try:
        existing = await client.post(
            "/api/v1/auth/register",
            json={
                "organization_name": "Oracle Probe HVAC",
                "full_name": "Real User",
                "email": known,
                "password": "super-secret-123",
            },
        )
        unknown = await client.post(
            "/api/v1/auth/register",
            json={
                "organization_name": "Oracle Probe HVAC",
                "full_name": "Real User",
                "email": "never-seen@example.com",
                "password": "super-secret-123",
            },
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_settings, None)

    assert existing.status_code == unknown.status_code == 403
    assert existing.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert existing.json()["error"]["message"] == unknown.json()["error"]["message"]
    # And the response must not echo the submitted address back.
    assert known not in existing.text


@pytest.mark.asyncio(loop_scope="session")
async def test_existing_users_can_still_log_in_while_registration_is_closed(
    client: AsyncClient,
):
    """Shutting the front door must not be an outage for the people already
    inside — otherwise it is unusable as a pilot control."""
    email = "still-works@example.com"
    password = "super-secret-123"
    created = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Still Works HVAC",
            "full_name": "Existing User",
            "email": email,
            "password": password,
        },
    )
    assert created.status_code == 201

    from app.core.config import get_settings
    from app.main import fastapi_app

    base = get_settings()
    fastapi_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"FEATURE_REGISTRATION_ENABLED": False}
    )
    try:
        login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_settings, None)

    assert login.status_code == 200
    assert login.json()["tokens"]["access_token"]


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_still_works_by_default(client: AsyncClient):
    """The flag defaults to true, so the shipped behaviour is unchanged."""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Default Open HVAC",
            "full_name": "Open Door",
            "email": "default-open@example.com",
            "password": "super-secret-123",
        },
    )

    assert response.status_code == 201
