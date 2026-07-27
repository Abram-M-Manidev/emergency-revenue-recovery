"""OpenAIProvider's timeout/retry-exhaustion handling, using a fake client
so this stays offline/deterministic — the one real-API exercise lives in
`tests/integration/test_openai_provider_smoke.py` (skipped unless
OPENAI_API_KEY is set)."""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.core.config import get_settings
from app.domain.ai.provider import AIRequest
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.ai.openai_provider import OpenAIProvider

_DUMMY_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


class _RaisingCompletions:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def create(self, **_kwargs):
        raise self._error


class _RaisingChat:
    def __init__(self, error: Exception) -> None:
        self.completions = _RaisingCompletions(error)


class _RaisingClient:
    def __init__(self, error: Exception) -> None:
        self.chat = _RaisingChat(error)


def _request() -> AIRequest:
    return AIRequest(
        system_prompt="You are a helpful assistant.",
        history=(),
        latest_customer_message="Hello?",
    )


async def _provider_raising(error: Exception, monkeypatch: pytest.MonkeyPatch) -> OpenAIProvider:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    provider._client = _RaisingClient(error)  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_timeout_maps_to_ai_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    provider = await _provider_raising(APITimeoutError(request=_DUMMY_REQUEST), monkeypatch)

    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request())


@pytest.mark.asyncio
async def test_connection_error_maps_to_ai_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    provider = await _provider_raising(APIConnectionError(request=_DUMMY_REQUEST), monkeypatch)

    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request())


@pytest.mark.asyncio
async def test_status_error_maps_to_ai_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    response = httpx.Response(500, request=_DUMMY_REQUEST)
    error = APIStatusError("Internal server error", response=response, body=None)
    provider = await _provider_raising(error, monkeypatch)

    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request())


@pytest.mark.asyncio
async def test_client_is_constructed_with_configured_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    settings = get_settings()
    provider = OpenAIProvider(settings)

    client = provider._get_client()

    assert client.timeout == settings.OPENAI_TIMEOUT_SECONDS
    assert client.max_retries == settings.OPENAI_MAX_RETRIES
