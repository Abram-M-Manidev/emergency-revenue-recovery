"""Deterministic tests for the voice-path telemetry added in H2.

Stream *abort* is tested here rather than through the HTTP layer on
purpose: closing an async generator with `aclose()` reproduces exactly
what Starlette does when a client disconnects, without depending on how a
particular test transport happens to tear a response down.

`structlog.testing.capture_logs` is deliberately not used — it swaps the
processor chain for a bare capture, which drops `merge_contextvars`, and
contextvar propagation is precisely the correlation mechanism under test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.api.v1.endpoints.vapi_webhooks import _instrumented_stream, _StreamStats
from tests.log_capture import capture_events


def _events(entries, name):
    return [e for e in entries if e.get("event") == name]


async def _frames(*values: str) -> AsyncIterator[str]:
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_fully_consumed_stream_reports_completed_and_not_aborted():
    stats = _StreamStats()
    stats.started_at = 0.0
    stats.content_frames = 3

    with capture_events() as entries:
        received = [frame async for frame in _instrumented_stream(stats, _frames("a", "b", "c"))]

    assert received == ["a", "b", "c"]
    assert len(_events(entries, "voice_stream_completed")) == 1
    assert _events(entries, "voice_stream_aborted") == []
    assert _events(entries, "voice_stream_completed")[0]["content_frames"] == 3


@pytest.mark.asyncio
async def test_abandoned_stream_reports_aborted_and_not_completed():
    """The pre-P4 failure shape: Vapi stops reading mid-turn."""
    stats = _StreamStats()
    stats.started_at = 0.0

    with capture_events() as entries:
        stream = _instrumented_stream(stats, _frames("a", "b", "c"))
        assert await stream.__anext__() == "a"
        stats.content_frames = 1
        await stream.aclose()

    assert len(_events(entries, "voice_stream_aborted")) == 1
    assert _events(entries, "voice_stream_completed") == []
    aborted = _events(entries, "voice_stream_aborted")[0]
    assert aborted["content_frames"] == 1
    assert aborted["reached_first_content"] is False


@pytest.mark.asyncio
async def test_instrumentation_does_not_buffer_or_reorder_frames():
    """P2 preservation: frames must arrive one at a time, in order, as the
    inner generator produces them — not collected and flushed at the end."""
    stats = _StreamStats()
    stats.started_at = 0.0
    seen_by_producer: list[str] = []

    async def producer() -> AsyncIterator[str]:
        for value in ("one", "two", "three"):
            seen_by_producer.append(value)
            yield value

    received: list[str] = []
    with capture_events():
        async for frame in _instrumented_stream(stats, producer()):
            # The producer must not have run ahead: each frame is consumed
            # before the next is produced.
            assert seen_by_producer == received + [frame]
            received.append(frame)

    assert received == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_abort_propagates_so_inner_cleanup_still_runs():
    """Swallowing GeneratorExit would strand the inner generator — and with
    it P1's advisory lock, which is released by the surrounding
    transaction ending."""
    cleaned_up = False

    async def producer() -> AsyncIterator[str]:
        nonlocal cleaned_up
        try:
            yield "a"
            yield "b"
        finally:
            cleaned_up = True

    stats = _StreamStats()
    stats.started_at = 0.0

    with capture_events():
        stream = _instrumented_stream(stats, producer())
        await stream.__anext__()
        await stream.aclose()

    assert cleaned_up is True


@pytest.mark.asyncio
async def test_error_in_stream_is_not_reported_as_an_abort():
    """A provider failure and a caller hang-up are different incidents and
    must not look identical in the log."""

    async def failing() -> AsyncIterator[str]:
        yield "a"
        raise RuntimeError("provider exploded")

    stats = _StreamStats()
    stats.started_at = 0.0

    with capture_events() as entries, pytest.raises(RuntimeError):
        async for _ in _instrumented_stream(stats, failing()):
            pass

    assert _events(entries, "voice_stream_aborted") == []
    assert _events(entries, "voice_stream_completed") == []
