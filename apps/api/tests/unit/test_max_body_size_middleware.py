from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.middleware import MaxBodySizeMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(MaxBodySizeMiddleware)

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"received": len(body)}

    return app


@pytest.mark.asyncio
async def test_allows_a_request_within_the_configured_limit():
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=b"x" * 100)

    assert response.status_code == 200
    assert response.json() == {"received": 100}


@pytest.mark.asyncio
async def test_rejects_a_request_over_the_configured_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "MAX_REQUEST_BODY_BYTES", 10)
    app = _build_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=b"x" * 100)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"
