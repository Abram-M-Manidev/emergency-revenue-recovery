"""Token usage capture in `OpenAIProvider`, and proof that capturing it
changes nothing a caller hears or any tool the model calls.

With `stream_options.include_usage`, OpenAI ends every stream with one
extra chunk whose `choices` list is EMPTY and whose `usage` is set. That
chunk must be counted and must never reach the content or tool-call
handling — in particular it must not disturb the content-first tool-call
ordering that once ended a real call on `silence-timed-out` (2026-08-22).

Driven by the same scripted client as `test_openai_tool_loop.py`."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from app.core.config import get_settings
from app.domain.ai.provider import AIModelProfile, AIRequest
from app.domain.ai.tools import CHECK_AVAILABILITY, CREATE_SERVICE_REQUEST
from app.domain.ai.usage import TokenUsage, TokenUsageSink, TokenUsageTally
from app.domain.exceptions import AIProviderUnavailableError
from app.infrastructure.ai.openai_provider import OpenAIProvider
from tests.unit.test_openai_tool_loop import (
    _REPLY_DOC,
    _REPLY_ROUND,
    _SPOKE_THEN_TOOL_ROUND,
    _chunk,
    _drain,
    _provider,
    _RecordingExecutor,
)


def _usage_chunk(prompt: int, completion: int, cached: int = 0):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


def _request(executor, sink: TokenUsageSink | None, *, with_tools: bool = True) -> AIRequest:
    return AIRequest(
        system_prompt="You are the after-hours assistant.",
        history=(),
        latest_customer_message="My AC is running but not cooling.",
        profile=AIModelProfile.REALTIME,
        tools=(CHECK_AVAILABILITY, CREATE_SERVICE_REQUEST) if with_tools else (),
        tool_executor=executor,
        usage_sink=sink,
    )


def _only_total(tally: TokenUsageTally):
    totals = tally.totals()
    assert len(totals) == 1
    return totals[0]


@pytest.mark.asyncio
async def test_every_streamed_round_asks_openai_for_usage(monkeypatch: pytest.MonkeyPatch):
    provider, client = _provider([_SPOKE_THEN_TOOL_ROUND, _REPLY_ROUND], monkeypatch)
    await _drain(provider, _request(_RecordingExecutor(), TokenUsageTally()))

    assert len(client.chat.completions.calls) == 2
    for call in client.chat.completions.calls:
        assert call["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_the_choice_less_usage_chunk_is_counted_and_never_spoken(
    monkeypatch: pytest.MonkeyPatch,
):
    tally = TokenUsageTally()
    provider, _ = _provider([[*_REPLY_ROUND, _usage_chunk(1200, 80, cached=1024)]], monkeypatch)

    deltas, phases, reply = await _drain(provider, _request(None, tally, with_tools=False))

    assert "".join(deltas) == "You're confirmed for Monday at 8 AM."
    assert phases == []
    assert reply is not None and reply.is_conversation_complete is True
    total = _only_total(tally)
    assert total.model == get_settings().OPENAI_REALTIME_MODEL
    assert (total.requests, total.requests_without_usage) == (1, 0)
    assert (total.prompt_tokens, total.cached_prompt_tokens, total.completion_tokens) == (
        1200,
        1024,
        80,
    )


# --- The 2026-08-22 regression, with usage chunks in the stream ---------------


_SPOKE_THEN_TOOL_WITH_USAGE = [*_SPOKE_THEN_TOOL_ROUND, _usage_chunk(900, 60)]
_REPLY_WITH_USAGE = [*_REPLY_ROUND, _usage_chunk(1100, 70, cached=896)]


@pytest.mark.asyncio
async def test_a_tool_call_after_spoken_content_still_runs_with_usage_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """The silence-timeout regression: the model speaks first, then asks for
    the tool. The usage chunk that now closes the round must not cost the
    tool call."""
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_WITH_USAGE, _REPLY_WITH_USAGE], monkeypatch)

    deltas, phases, reply = await _drain(provider, _request(executor, TokenUsageTally()))

    assert [i.name for i in executor.invocations] == ["check_availability"]
    assert executor.invocations[0].arguments == {"service_name": "Air Conditioning Repair"}
    assert "".join(deltas).startswith("I'll check our availability for you now.")
    assert len(phases) == 1 and phases[0].model_already_spoke is True
    assert reply is not None
    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."


@pytest.mark.asyncio
async def test_usage_is_summed_across_the_tool_rounds_of_one_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    tally = TokenUsageTally()
    provider, _ = _provider([_SPOKE_THEN_TOOL_WITH_USAGE, _REPLY_WITH_USAGE], monkeypatch)

    await _drain(provider, _request(_RecordingExecutor(), tally))

    total = _only_total(tally)
    assert (total.requests, total.requests_without_usage) == (2, 0)
    assert (total.prompt_tokens, total.cached_prompt_tokens, total.completion_tokens) == (
        2000,
        896,
        130,
    )


@pytest.mark.asyncio
async def test_identical_events_with_and_without_metering(monkeypatch: pytest.MonkeyPatch):
    """Metering is observation only: the caller-facing event stream is
    byte-identical whether or not a sink is attached."""
    metered, _ = _provider([_SPOKE_THEN_TOOL_WITH_USAGE, _REPLY_WITH_USAGE], monkeypatch)
    with_sink = await _drain(metered, _request(_RecordingExecutor(), TokenUsageTally()))
    unmetered, _ = _provider([_SPOKE_THEN_TOOL_WITH_USAGE, _REPLY_WITH_USAGE], monkeypatch)
    without_sink = await _drain(unmetered, _request(_RecordingExecutor(), None))

    assert with_sink == without_sink


# --- Undercounts are visible, never silent zeros -----------------------------


@pytest.mark.asyncio
async def test_a_stream_without_a_usage_chunk_counts_the_request_as_unreported(
    monkeypatch: pytest.MonkeyPatch,
):
    tally = TokenUsageTally()
    provider, _ = _provider([_REPLY_ROUND], monkeypatch)

    await _drain(provider, _request(None, tally, with_tools=False))

    total = _only_total(tally)
    assert (total.requests, total.requests_without_usage) == (1, 1)
    assert (total.prompt_tokens, total.completion_tokens) == (0, 0)


@pytest.mark.asyncio
async def test_an_abandoned_stream_still_counts_its_request(monkeypatch: pytest.MonkeyPatch):
    """Vapi abandoning the turn closes the generator mid-round: the request
    was sent and is counted, as one whose usage never arrived."""
    tally = TokenUsageTally()
    pieces = [{"content": _REPLY_DOC[i : i + 30]} for i in range(0, len(_REPLY_DOC), 30)]
    provider, _ = _provider([[_chunk(**piece) for piece in pieces]], monkeypatch)
    stream = provider.stream_reply(_request(None, tally, with_tools=False))
    await stream.__anext__()
    await stream.aclose()

    total = _only_total(tally)
    assert (total.requests, total.requests_without_usage) == (1, 1)


class _FailingStreamCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        raise APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/x"))


@pytest.mark.asyncio
async def test_a_failed_request_is_counted_without_usage_and_still_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    provider._clients[AIModelProfile.REALTIME] = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=_FailingStreamCompletions())
    )
    tally = TokenUsageTally()

    with pytest.raises(AIProviderUnavailableError):
        await _drain(provider, _request(None, tally, with_tools=False))
    with pytest.raises(AIProviderUnavailableError):
        await provider.generate_reply(_request(None, tally, with_tools=False))

    total = _only_total(tally)
    assert (total.requests, total.requests_without_usage) == (2, 2)


class _ExplodingSink(TokenUsageSink):
    def record(self, usage: TokenUsage) -> None:
        raise RuntimeError("metering is broken")


@pytest.mark.asyncio
async def test_a_broken_sink_never_costs_the_turn(monkeypatch: pytest.MonkeyPatch):
    executor = _RecordingExecutor()
    provider, _ = _provider([_SPOKE_THEN_TOOL_WITH_USAGE, _REPLY_WITH_USAGE], monkeypatch)

    deltas, _, reply = await _drain(provider, _request(executor, _ExplodingSink()))

    assert [i.name for i in executor.invocations] == ["check_availability"]
    assert reply is not None
    assert "".join(deltas).endswith("You're confirmed for Monday at 8 AM.")


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(prompt_tokens=-5, completion_tokens="ten"),
        SimpleNamespace(prompt_tokens=True, completion_tokens=None),
        SimpleNamespace(),
    ],
)
@pytest.mark.asyncio
async def test_malformed_usage_is_counted_as_unreported(monkeypatch, usage):
    tally = TokenUsageTally()
    provider, _ = _provider(
        [[*_REPLY_ROUND, SimpleNamespace(choices=[], usage=usage)]], monkeypatch
    )

    await _drain(provider, _request(None, tally, with_tools=False))

    total = _only_total(tally)
    assert (total.requests_without_usage, total.prompt_tokens, total.completion_tokens) == (1, 0, 0)


# --- Non-streaming path ------------------------------------------------------


class _CompletionWithUsage:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_REPLY_DOC, tool_calls=None))],
            usage=SimpleNamespace(
                prompt_tokens=700,
                completion_tokens=40,
                prompt_tokens_details=SimpleNamespace(cached_tokens=512),
            ),
        )


@pytest.mark.asyncio
async def test_the_non_streaming_path_records_usage(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "OPENAI_API_KEY", "sk-fake-key-for-testing")
    provider = OpenAIProvider(get_settings())
    completions = _CompletionWithUsage()
    provider._clients[AIModelProfile.REALTIME] = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=completions)
    )
    tally = TokenUsageTally()

    reply = await provider.generate_reply(_request(None, tally, with_tools=False))

    assert reply.message_to_customer == "You're confirmed for Monday at 8 AM."
    assert "stream_options" not in completions.calls[0]
    total = _only_total(tally)
    assert (total.requests, total.prompt_tokens, total.cached_prompt_tokens, total.completion_tokens) == (
        1,
        700,
        512,
        40,
    )
