"""OpenAIProvider's timeout/retry-exhaustion handling, using a fake client
so this stays offline/deterministic — the one real-API exercise lives in
`tests/integration/test_openai_provider_smoke.py` (skipped unless
OPENAI_API_KEY is set)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.core.config import get_settings
from app.domain.ai.provider import (
    AIModelProfile,
    AIReplyComplete,
    AIRequest,
    AITextDelta,
)
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction
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


class _RecordingCompletions:
    """Captures the kwargs the provider actually sends, so the profile ->
    model/reasoning_effort mapping can be asserted without a network call."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        raise APITimeoutError(request=_DUMMY_REQUEST)


class _RecordingChat:
    def __init__(self) -> None:
        self.completions = _RecordingCompletions()


class _RecordingClient:
    def __init__(self) -> None:
        self.chat = _RecordingChat()


def _request(profile: AIModelProfile = AIModelProfile.QUALITY) -> AIRequest:
    return AIRequest(
        system_prompt="You are a helpful assistant.",
        history=(),
        latest_customer_message="Hello?",
        profile=profile,
    )


async def _provider_raising(error: Exception, monkeypatch: pytest.MonkeyPatch) -> OpenAIProvider:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    provider._clients[AIModelProfile.QUALITY] = _RaisingClient(error)  # type: ignore[assignment]
    return provider


async def _capture_request_kwargs(
    profile: AIModelProfile, monkeypatch: pytest.MonkeyPatch
) -> dict:
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    client = _RecordingClient()
    provider._clients[profile] = client  # type: ignore[assignment]
    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request(profile))
    return client.chat.completions.kwargs


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

    client = provider._get_client(AIModelProfile.QUALITY)

    assert client.timeout == settings.OPENAI_TIMEOUT_SECONDS
    assert client.max_retries == settings.OPENAI_MAX_RETRIES


@pytest.mark.asyncio
async def test_realtime_client_uses_its_own_timeout_and_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    """A live call must fail fast into the speakable fallback rather than
    hold the caller through the (much longer) text-path timeout, and must
    not silently re-bill a turn the caller may have abandoned."""
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    settings = get_settings()
    provider = OpenAIProvider(settings)

    client = provider._get_client(AIModelProfile.REALTIME)

    assert client.timeout == settings.OPENAI_REALTIME_TIMEOUT_SECONDS
    assert client.max_retries == settings.OPENAI_REALTIME_MAX_RETRIES
    assert settings.OPENAI_REALTIME_MAX_RETRIES == 0
    assert settings.OPENAI_REALTIME_TIMEOUT_SECONDS < settings.OPENAI_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_each_profile_gets_its_own_cached_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())

    quality = provider._get_client(AIModelProfile.QUALITY)
    realtime = provider._get_client(AIModelProfile.REALTIME)

    assert quality is not realtime
    assert provider._get_client(AIModelProfile.QUALITY) is quality


@pytest.mark.asyncio
async def test_quality_profile_sends_configured_model_and_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
):
    settings = get_settings()
    kwargs = await _capture_request_kwargs(AIModelProfile.QUALITY, monkeypatch)

    assert kwargs["model"] == settings.OPENAI_MODEL
    assert kwargs["extra_body"] == {"reasoning_effort": settings.OPENAI_REASONING_EFFORT}
    assert settings.OPENAI_MODEL == "gpt-5"
    assert settings.OPENAI_REASONING_EFFORT == "low"


@pytest.mark.asyncio
async def test_realtime_profile_sends_fast_model_and_omits_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
):
    """gpt-4.1-mini is not a reasoning model — sending the parameter would
    be a 400, so it must be absent rather than null."""
    settings = get_settings()
    kwargs = await _capture_request_kwargs(AIModelProfile.REALTIME, monkeypatch)

    assert kwargs["model"] == settings.OPENAI_REALTIME_MODEL
    assert kwargs["extra_body"] is None
    assert settings.OPENAI_REALTIME_MODEL == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_structured_output_schema_is_unchanged_across_profiles(
    monkeypatch: pytest.MonkeyPatch,
):
    """The AI Brain contract (strict JSON schema, emergency classification,
    recommended_action) must be identical no matter which model answers."""
    quality = await _capture_request_kwargs(AIModelProfile.QUALITY, monkeypatch)
    realtime = await _capture_request_kwargs(AIModelProfile.REALTIME, monkeypatch)

    assert quality["response_format"] == realtime["response_format"]
    schema = quality["response_format"]["json_schema"]
    assert schema["strict"] is True
    required = schema["schema"]["required"]
    assert "classification" in required
    assert "recommended_action" in required


# --- P2: genuine upstream streaming -----------------------------------------
#
# `stream_reply` forwards `message_to_customer` as OpenAI writes it, but must
# take every decision field from the complete, validated document. These use a
# fake OpenAI stream so the chunk boundaries are chosen deliberately rather
# than left to a live model.


_STREAM_DOC = json.dumps(
    {
        "message_to_customer": "Help is on the way. Please stay safe.",
        "classification": "emergency",
        "confidence": 0.97,
        "recommended_action": "create_emergency_ticket",
        "matched_service_name": "Emergency No-Heat Repair",
        "customer_name": "Lucky",
        "customer_phone": "123456789",
        "customer_address": "1600 Street, California",
        "is_conversation_complete": True,
        "summary": "No heat, emergency dispatch required.",
    }
)


def _chunk(text: str | None):
    delta = SimpleNamespace(content=text)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _FakeOpenAIStream:
    """Async-iterable stand-in for `AsyncStream[ChatCompletionChunk]`."""

    def __init__(self, pieces: list[str], *, fail_with: Exception | None = None) -> None:
        self._pieces = pieces
        self._fail_with = fail_with

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for piece in self._pieces:
            yield _chunk(piece)
        if self._fail_with is not None:
            raise self._fail_with


class _StreamingCompletions:
    def __init__(self, pieces: list[str], *, fail_with: Exception | None = None) -> None:
        self._pieces = pieces
        self._fail_with = fail_with
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeOpenAIStream(self._pieces, fail_with=self._fail_with)


class _StreamingClient:
    def __init__(self, pieces: list[str], *, fail_with: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=_StreamingCompletions(pieces, fail_with=fail_with))


async def _collect_stream(
    pieces: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: AIModelProfile = AIModelProfile.REALTIME,
    fail_with: Exception | None = None,
):
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    client = _StreamingClient(pieces, fail_with=fail_with)
    provider._clients[profile] = client  # type: ignore[assignment]
    deltas: list[str] = []
    reply = None
    async for event in provider.stream_reply(_request(profile)):
        if isinstance(event, AITextDelta):
            deltas.append(event.text)
        elif isinstance(event, AIReplyComplete):
            reply = event.reply
    return deltas, reply, client.chat.completions.kwargs


@pytest.mark.asyncio
async def test_stream_reply_requests_streaming_from_openai(monkeypatch: pytest.MonkeyPatch):
    """A. Not fake streaming — `stream=True` must reach the SDK."""
    _, _, kwargs = await _collect_stream([_STREAM_DOC], monkeypatch)
    assert kwargs["stream"] is True
    assert kwargs["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_stream_reply_emits_progressive_text_deltas(monkeypatch: pytest.MonkeyPatch):
    """K. Multiple content deltas, emitted as the document arrives."""
    pieces = [_STREAM_DOC[i : i + 12] for i in range(0, len(_STREAM_DOC), 12)]
    deltas, reply, _ = await _collect_stream(pieces, monkeypatch)

    assert len(deltas) > 1, "expected progressive deltas, not one buffered blob"
    assert "".join(deltas) == "Help is on the way. Please stay safe."
    assert reply is not None


@pytest.mark.asyncio
async def test_decision_fields_come_only_from_the_complete_document(
    monkeypatch: pytest.MonkeyPatch,
):
    """F., G., H., I. — the authoritative contract."""
    pieces = [_STREAM_DOC[i : i + 7] for i in range(0, len(_STREAM_DOC), 7)]
    _, reply, _ = await _collect_stream(pieces, monkeypatch)

    assert reply is not None
    assert reply.classification is CallClassification.EMERGENCY
    assert reply.recommended_action is RecommendedAction.CREATE_EMERGENCY_TICKET
    assert reply.is_conversation_complete is True
    assert reply.confidence == 0.97
    assert reply.matched_service_name == "Emergency No-Heat Repair"
    assert reply.customer_name == "Lucky"
    assert reply.customer_phone == "123456789"
    assert reply.customer_address == "1600 Street, California"
    assert reply.message_to_customer == "Help is on the way. Please stay safe."


@pytest.mark.asyncio
async def test_no_json_syntax_ever_reaches_the_caller(monkeypatch: pytest.MonkeyPatch):
    """The caller must never hear braces, keys, or decision values."""
    pieces = list(_STREAM_DOC)
    deltas, _, _ = await _collect_stream(pieces, monkeypatch)
    spoken = "".join(deltas)

    for forbidden in ["{", "}", '"', "classification", "emergency", "summary", "confidence"]:
        assert forbidden not in spoken


@pytest.mark.asyncio
async def test_truncated_stream_raises_rather_than_persisting_partial(
    monkeypatch: pytest.MonkeyPatch,
):
    """M. A stream that dies mid-document must not yield a complete event —
    otherwise a half-formed outcome would be persisted. `json.loads` raises
    `JSONDecodeError` (a `ValueError`) on the truncated document."""
    truncated = _STREAM_DOC[: len(_STREAM_DOC) // 2]
    with pytest.raises(ValueError):
        await _collect_stream([truncated], monkeypatch)


@pytest.mark.asyncio
async def test_stream_transport_failure_maps_to_ai_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """M. Transport errors surface as the domain error the webhook already
    turns into a speakable fallback."""
    with pytest.raises(AIProviderUnavailableError):
        await _collect_stream(
            ['{"message_to_customer": "partial'],
            monkeypatch,
            fail_with=APITimeoutError(request=_DUMMY_REQUEST),
        )


@pytest.mark.asyncio
async def test_empty_stream_raises_ai_provider_unavailable(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(AIProviderUnavailableError):
        await _collect_stream([], monkeypatch)


@pytest.mark.asyncio
async def test_default_stream_reply_works_for_non_streaming_providers():
    """The default implementation keeps every existing provider correct
    without modification — this is what protects the text path and the
    fakes."""
    from tests.fakes import FakeAIProvider, default_reply

    provider = FakeAIProvider()
    provider.queue_reply(default_reply(message_to_customer="single shot"))

    deltas: list[str] = []
    reply = None
    async for event in provider.stream_reply(_request()):
        if isinstance(event, AITextDelta):
            deltas.append(event.text)
        else:
            reply = event.reply

    assert deltas == ["single shot"]
    assert reply is not None
    assert reply.message_to_customer == "single shot"
