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
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware

_VAPI_SECRET = "test-vapi-secret-for-rate-limit-middleware"


@pytest.fixture
def non_testing_settings():
    original_environment = os.environ.get("ENVIRONMENT")
    original_secret = os.environ.get("VAPI_SERVER_SECRET")
    os.environ["ENVIRONMENT"] = "development"
    # Pinned to a known value so the Vapi-exemption tests are deterministic
    # rather than depending on whatever secret the environment happens to
    # carry (and so a real secret never has to be read here).
    os.environ["VAPI_SERVER_SECRET"] = _VAPI_SECRET
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        for name, original in (
            ("ENVIRONMENT", original_environment),
            ("VAPI_SERVER_SECRET", original_secret),
        ):
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
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

    @app.post("/api/v1/voice/vapi/chat/completions")
    async def vapi_chat_completions() -> StreamingResponse:
        # Mirrors production: the live webhook answers with SSE, which is
        # what makes an unwanted 429 (a JSON envelope) so damaging.
        async def _frames():
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(_frames(), media_type="text/event-stream")

    @app.post("/api/v1/voice/vapi/events")
    async def vapi_events() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _vapi_headers() -> dict[str, str]:
    return {"x-vapi-secret": _VAPI_SECRET}


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


# --- H1: authenticated Vapi webhooks bypass the shared IP bucket ---


@pytest.mark.asyncio
async def test_authenticated_vapi_webhook_is_never_throttled(non_testing_settings):
    """One live call can easily exceed the default tier on its own; many
    concurrent calls share the same Vapi egress IP and certainly would."""
    app = _build_app()
    over_limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE + 25
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(over_limit):
            response = await client.post(
                "/api/v1/voice/vapi/chat/completions", headers=_vapi_headers()
            )
            assert response.status_code == 200


@pytest.mark.asyncio
async def test_authenticated_vapi_webhook_still_returns_sse_past_the_limit(
    non_testing_settings,
):
    """The damage a 429 does here is transport-shaped: Vapi would receive
    a JSON envelope where it expects an event stream."""
    app = _build_app()
    over_limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE + 5
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(over_limit):
            response = await client.post(
                "/api/v1/voice/vapi/chat/completions", headers=_vapi_headers()
            )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text


@pytest.mark.asyncio
async def test_vapi_webhook_without_the_secret_is_still_rate_limited(non_testing_settings):
    app = _build_app()
    limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(limit):
            response = await client.post("/api/v1/voice/vapi/chat/completions")
            assert response.status_code == 200

        breached = await client.post("/api/v1/voice/vapi/chat/completions")

    assert breached.status_code == 429
    assert breached.json()["error"]["code"] == "RATE_LIMITED"
    assert "Retry-After" in breached.headers


@pytest.mark.asyncio
async def test_vapi_webhook_with_a_wrong_secret_is_still_rate_limited(non_testing_settings):
    app = _build_app()
    limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE
    transport = ASGITransport(app=app)
    wrong = {"x-vapi-secret": "not-the-configured-secret"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(limit):
            assert (
                await client.post("/api/v1/voice/vapi/chat/completions", headers=wrong)
            ).status_code == 200

        breached = await client.post("/api/v1/voice/vapi/chat/completions", headers=wrong)

    assert breached.status_code == 429


@pytest.mark.asyncio
async def test_authenticated_vapi_traffic_neither_consumes_nor_consults_the_ip_bucket(
    non_testing_settings,
):
    """The core H1 property. Two directions, because either one failing
    would still let one caller's traffic throttle another's:

    1. An exhausted default tier (same client IP) must not block Vapi.
    2. Vapi's own volume must not be what exhausted it.
    """
    app = _build_app()
    limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Vapi runs well past the limit first — none of it may count.
        for _ in range(limit + 10):
            await client.post("/api/v1/voice/vapi/chat/completions", headers=_vapi_headers())

        # The ordinary API tier is therefore still untouched.
        assert (await client.get("/api/v1/customers")).status_code == 200

        # Now exhaust the ordinary tier from the very same IP...
        for _ in range(limit):
            await client.get("/api/v1/customers")
        assert (await client.get("/api/v1/customers")).status_code == 429

        # ...and Vapi is still unaffected by it.
        response = await client.post(
            "/api/v1/voice/vapi/chat/completions", headers=_vapi_headers()
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_both_vapi_routes_are_exempt(non_testing_settings):
    """The events webhook shares the prefix and must behave identically —
    an end-of-call report arriving late must never be thrown away."""
    app = _build_app()
    over_limit = non_testing_settings.RATE_LIMIT_DEFAULT_PER_MINUTE + 5
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(over_limit):
            assert (
                await client.post("/api/v1/voice/vapi/events", headers=_vapi_headers())
            ).status_code == 200
