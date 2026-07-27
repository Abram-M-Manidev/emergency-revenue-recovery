import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app


@pytest.mark.asyncio
async def test_liveness_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_version_returns_version_and_environment():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/version")

    body = response.json()
    assert response.status_code == 200
    assert body["environment"] == "testing"
    assert "version" in body


@pytest.mark.asyncio
async def test_real_app_serves_security_headers_and_a_request_id():
    """Confirms SecurityHeadersMiddleware/RequestIDMiddleware are actually
    wired into `app.main.create_app()`, not just unit-testable in
    isolation (see `tests/unit/test_security_headers_middleware.py` for
    the exhaustive per-header/HSTS-in-production cases)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_real_app_rejects_an_oversized_request_body():
    """Confirms MaxBodySizeMiddleware is wired into the real app (exhaustive
    cases live in `tests/unit/test_max_body_size_middleware.py`). Targets
    `/auth/register` only because it's a simple, unauthenticated POST route
    — the oversized body is rejected by `Content-Length` before the request
    ever reaches `AuthService`, so this never touches the database."""
    settings = get_settings()
    oversized_body = b"x" * (settings.MAX_REQUEST_BODY_BYTES + 1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/register",
            content=oversized_body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"
