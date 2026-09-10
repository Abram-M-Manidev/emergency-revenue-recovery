from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.middleware import SecurityHeadersMiddleware


@pytest.fixture
def production_settings():
    """A valid, fail-fast-validator-satisfying production configuration —
    needed to exercise the HSTS-only-in-production branch without
    tripping `Settings._validate_production_safety`."""
    keys = [
        "ENVIRONMENT",
        "DEBUG",
        "JWT_SECRET_KEY",
        "CORS_ORIGINS",
        # Both became production requirements when the validator was
        # strengthened: each fails closed at runtime (an unset webhook secret
        # rejects every Vapi call, a missing API key fails every turn) while
        # leaving the deployment looking healthy, so production now refuses
        # to boot without them. Set here so this fixture keeps describing a
        # *valid* production config, and so the run does not depend on
        # whatever the ambient environment happens to carry.
        "VAPI_SERVER_SECRET",
        "OPENAI_API_KEY",
    ]
    originals = {key: os.environ.get(key) for key in keys}
    os.environ["ENVIRONMENT"] = "production"
    os.environ["DEBUG"] = "false"
    os.environ["JWT_SECRET_KEY"] = "a-real-looking-production-secret-not-the-placeholder-value"
    os.environ["CORS_ORIGINS"] = "https://example.com"
    os.environ["VAPI_SERVER_SECRET"] = "a-real-webhook-shared-secret"
    os.environ["OPENAI_API_KEY"] = "sk-not-a-real-key-for-tests-only"
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_baseline_headers_present_on_every_response():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Permissions-Policy"] == "geolocation=(), microphone=(), camera=()"


@pytest.mark.asyncio
async def test_hsts_absent_outside_production():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping")

    assert "Strict-Transport-Security" not in response.headers


@pytest.mark.asyncio
async def test_hsts_present_in_production(production_settings):
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ping")

    assert response.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
