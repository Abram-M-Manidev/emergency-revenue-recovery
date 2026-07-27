"""`RateLimitMiddleware` bypasses enforcement entirely when
`settings.is_testing` (see its own docstring in `app/core/middleware.py`)
— necessary because the ~15 integration test files that each
register/log in at least once would otherwise collectively trip the
auth-tier limit within a single fast pytest run (they all share one
client identity, since register/login are unauthenticated and the test
client's IP never changes).

These tests prove real enforcement by temporarily flipping `ENVIRONMENT`
away from "testing" and driving requests through a small standalone app
built just for this file — never the shared `app.main` app used by every
other integration test.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware


@pytest.fixture
def non_testing_settings():
    original_environment = os.environ.get("ENVIRONMENT")
    os.environ["ENVIRONMENT"] = "development"
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        if original_environment is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = original_environment
        get_settings.cache_clear()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/v1/customers")
    async def customers() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_auth_tier_returns_429_past_the_limit(non_testing_settings):
    app = _build_app()
    limit = non_testing_settings.RATE_LIMIT_AUTH_PER_MINUTE
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(limit):
            response = await client.post("/api/v1/auth/login")
            assert response.status_code == 200

        breached = await client.post("/api/v1/auth/login")

    assert breached.status_code == 429
    assert breached.json()["error"]["code"] == "RATE_LIMITED"
    assert "Retry-After" in breached.headers


@pytest.mark.asyncio
async def test_health_is_exempt_from_rate_limiting(non_testing_settings):
    app = _build_app()
    limit = non_testing_settings.RATE_LIMIT_AUTH_PER_MINUTE
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(limit + 5):
            response = await client.get("/api/v1/health")
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_default_tier_is_independent_from_auth_tier(non_testing_settings):
    app = _build_app()
    auth_limit = non_testing_settings.RATE_LIMIT_AUTH_PER_MINUTE
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(auth_limit):
            await client.post("/api/v1/auth/login")
        breached = await client.post("/api/v1/auth/login")
        assert breached.status_code == 429

        # Same client (same IP key), but the default tier is a separate
        # counter keyed independently of the exhausted auth tier.
        response = await client.get("/api/v1/customers")

    assert response.status_code == 200
