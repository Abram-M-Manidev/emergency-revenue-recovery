"""Authentication edge cases found in the 2026-09-25 production audit.

Each of these returned a 500 or leaked information against real Postgres,
and none could be seen through the in-memory fakes: two are column widths
and byte limits, the third is a timing difference.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.application.services import auth_service
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.refresh_token import RefreshTokenModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app

# 40 two-byte characters: 40 characters (passes `max_length=72`), 80 bytes
# (over bcrypt's 72-byte limit).
_MULTIBYTE_PASSWORD = "é" * 40


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


async def _register(client: AsyncClient, email: str, **headers: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": f"Auth Hardening {email}",
            "full_name": "Owner",
            "email": email,
            "password": "super-secret-123",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio(loop_scope="session")
async def test_a_long_user_agent_does_not_break_login_or_refresh(client: AsyncClient):
    """`refresh_tokens.user_agent` is varchar(255) and the header is the
    client's. In-app browsers routinely send more; login then failed with a
    raw database error for exactly those users."""
    long_agent = "Mozilla/5.0 (Linux; Android 14) " + "FBAV/450.0.0.0;" * 30
    assert len(long_agent) > 255
    await _register(client, "long-agent@example.com", **{"user-agent": long_agent})

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "long-agent@example.com", "password": "super-secret-123"},
        headers={"user-agent": long_agent},
    )
    assert login.status_code == 200, login.text

    refresh = await client.post("/api/v1/auth/refresh", headers={"user-agent": long_agent})
    assert refresh.status_code == 200, refresh.text

    async with AsyncSessionLocal() as session:
        agents = (await session.execute(select(RefreshTokenModel.user_agent))).scalars().all()
    assert agents and all(agent is None or len(agent) <= 255 for agent in agents)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_password_over_bcrypts_byte_limit_is_a_422_not_a_500(client: AsyncClient):
    register = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Multibyte Org",
            "full_name": "Owner",
            "email": "multibyte@example.com",
            "password": _MULTIBYTE_PASSWORD,
        },
    )
    assert register.status_code == 422, register.text

    owner = await _register(client, "multibyte-owner@example.com")
    token = owner["tokens"]["access_token"]
    invite = await client.post(
        "/api/v1/team/members",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "full_name": "Member",
            "email": "multibyte-member@example.com",
            "temporary_password": _MULTIBYTE_PASSWORD,
            "role": "Member",
        },
    )
    assert invite.status_code == 422, invite.text


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_email_costs_the_same_bcrypt_work_as_a_known_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """An unknown address used to return before hashing anything — tens of
    microseconds against ~250ms — which answers "does this account exist?"
    as reliably as a different error message would."""
    await _register(client, "timing-known@example.com")

    calls: list[str] = []
    original = auth_service.verify_password

    def counting_verify(plain: str, hashed: str) -> bool:
        calls.append(hashed)
        return original(plain, hashed)

    monkeypatch.setattr(auth_service, "verify_password", counting_verify)

    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "timing-unknown@example.com", "password": "super-secret-123"},
    )
    known_wrong = await client.post(
        "/api/v1/auth/login",
        json={"email": "timing-known@example.com", "password": "wrong-password-1"},
    )

    assert unknown.status_code == known_wrong.status_code == 401
    assert unknown.json()["error"]["code"] == known_wrong.json()["error"]["code"]
    assert len(calls) == 2
    assert all(hashed.startswith("$2") for hashed in calls)
