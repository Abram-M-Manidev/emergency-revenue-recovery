"""A refresh cookie that can never work again is cleared, not left behind.

The frontend's edge middleware only sees whether the `refresh_token` cookie
EXISTS (it cannot verify it), so a dead cookie used to trap the visitor: `/`
and `/login` redirected to `/dashboard`, whose refresh failed, whose guard
sent them to `/login`, which redirected back — an endless spinner. The fix is
that every refresh failure meaning "this token is finished" now clears the
cookie with the exact attributes it was set with.

Real Postgres, real routes. Each test drives the cookie by hand (the
client's own jar is cleared first) so what the browser would hold is
explicit.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import update

from app.api.deps import get_auth_service
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.refresh_token import RefreshTokenModel
from app.infrastructure.database.models.user import UserModel
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app

_REFRESH = "/api/v1/auth/refresh"
_COOKIE = "refresh_token"


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
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    # `raise_app_exceptions=False` so an unhandled server error comes back
    # as the 500 a browser would see, instead of raising in the test.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient) -> tuple[str, str, uuid.UUID]:
    """A fresh owner. Returns (refresh cookie value, email, user id)."""
    email = f"stale-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post("/api/v1/auth/register", json={
        "organization_name": f"Stale Cookie {email[6:12]}",
        "full_name": "Stale Cookie Test",
        "email": email,
        "password": "super-secret-123",
    })
    assert response.status_code == 201, response.text
    token = response.cookies[_COOKIE]
    client.cookies.clear()
    return token, email, uuid.UUID(response.json()["user"]["id"])


async def _refresh(client: AsyncClient, token: str | None) -> Response:
    client.cookies.clear()
    headers = {"Cookie": f"{_COOKIE}={token}"} if token is not None else {}
    response = await client.post(_REFRESH, headers=headers)
    client.cookies.clear()
    return response


def _refresh_set_cookies(response: Response) -> list[str]:
    return [h for h in response.headers.get_list("set-cookie") if h.startswith(f"{_COOKIE}=")]


def _assert_cleared(response: Response) -> None:
    [header] = _refresh_set_cookies(response)
    attributes = {part.strip().lower() for part in header.split(";")}
    assert header.startswith(f'{_COOKIE}="";') or header.startswith(f"{_COOKIE}=;")
    assert "max-age=0" in attributes
    # The same attributes `_set_refresh_cookie` uses, or the browser would
    # not match — and would keep — the cookie being cleared.
    assert "path=/" in attributes
    assert "httponly" in attributes
    assert "samesite=lax" in attributes
    assert not any(part.startswith("domain=") for part in attributes)


def _assert_issued(response: Response) -> None:
    [header] = _refresh_set_cookies(response)
    attributes = {part.strip().lower() for part in header.split(";")}
    assert "max-age=0" not in attributes
    assert header.split(";", 1)[0] != f"{_COOKIE}="


# --- the session is finished: cookie cleared ---------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_malformed_cookie_is_cleared(client):
    response = await _refresh(client, "stale-review-test-not-a-real-token")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_well_formed_but_unknown_token_is_cleared(client):
    token, _, _ = await _register(client)
    # Same shape as a real token, different secret: no such row.
    prefix, _, secret = token.rpartition(".")
    forged = f"{prefix}.{'x' * len(secret)}" if prefix else "x" * len(token)

    response = await _refresh(client, forged)

    assert response.status_code == 401
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_revoked_token_is_cleared(client):
    token, _, _ = await _register(client)
    client.cookies.clear()
    logout = await client.post("/api/v1/auth/logout", headers={"Cookie": f"{_COOKIE}={token}"})
    assert logout.status_code == 204

    response = await _refresh(client, token)

    assert response.status_code == 401
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_already_rotated_token_is_cleared(client):
    """Refreshing rotates the token. A second tab still holding the old
    value is exactly the stale-cookie visitor."""
    old, _, _ = await _register(client)
    first = await _refresh(client, old)
    assert first.status_code == 200
    _assert_issued(first)

    response = await _refresh(client, old)

    assert response.status_code == 401
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_expired_token_is_cleared(client):
    token, _, user_id = await _register(client)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.user_id == user_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()

    response = await _refresh(client, token)

    assert response.status_code == 401
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivated_user_s_token_is_cleared(client):
    token, _, user_id = await _register(client)
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(UserModel).where(UserModel.id == user_id).values(is_active=False)
        )
        await session.commit()

    response = await _refresh(client, token)

    assert response.status_code == 401
    _assert_cleared(response)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivated_organization_s_token_is_cleared(client):
    token, _, user_id = await _register(client)
    async with AsyncSessionLocal() as session:
        user = await session.get(UserModel, user_id)
        assert user is not None
        await session.execute(
            update(OrganizationModel)
            .where(OrganizationModel.id == user.organization_id)
            .values(is_active=False)
        )
        await session.commit()

    response = await _refresh(client, token)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INACTIVE_ACCOUNT"
    _assert_cleared(response)


# --- nothing to clear, or not the session's fault: cookie untouched ----------


@pytest.mark.asyncio(loop_scope="session")
async def test_no_cookie_means_nothing_is_set(client):
    response = await _refresh(client, None)

    assert response.status_code == 401
    assert _refresh_set_cookies(response) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_a_server_fault_keeps_the_cookie(client):
    """A transient failure (here: the auth service itself breaking) must
    not sign anyone out — only a finished session clears the cookie."""

    class _Broken:
        async def refresh(self, **_kwargs):
            raise RuntimeError("database is down")

    fastapi_app.dependency_overrides[get_auth_service] = lambda: _Broken()
    try:
        response = await _refresh(client, "any-token-value")
    finally:
        fastapi_app.dependency_overrides.pop(get_auth_service, None)

    assert response.status_code == 500
    assert _refresh_set_cookies(response) == []


# --- normal behaviour is unchanged -------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_valid_token_still_refreshes_and_rotates(client):
    token, email, _ = await _register(client)

    response = await _refresh(client, token)

    assert response.status_code == 200
    assert response.json()["user"]["email"] == email
    _assert_issued(response)
    new_token = _refresh_set_cookies(response)[0].split(";", 1)[0].split("=", 1)[1]
    assert new_token and new_token != token


@pytest.mark.asyncio(loop_scope="session")
async def test_logout_still_clears_the_cookie(client):
    token, _, _ = await _register(client)
    client.cookies.clear()

    response = await client.post("/api/v1/auth/logout", headers={"Cookie": f"{_COOKIE}={token}"})

    assert response.status_code == 204
    [header] = _refresh_set_cookies(response)
    assert "max-age=0" in header.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_clearing_the_cookie_never_grants_access(client):
    """The fix changes only what the browser keeps. Protected data still
    needs a valid bearer token — never a cookie, stale or fresh."""
    token, _, _ = await _register(client)
    cleared = await _refresh(client, "stale-review-test-not-a-real-token")
    assert cleared.status_code == 401

    for path in ("/api/v1/auth/me", "/api/v1/customers", "/api/v1/organizations/current"):
        client.cookies.clear()
        no_auth = await client.get(path)
        cookie_only = await client.get(path, headers={"Cookie": f"{_COOKIE}={token}"})
        assert no_auth.status_code == 401, path
        assert cookie_only.status_code == 401, path
