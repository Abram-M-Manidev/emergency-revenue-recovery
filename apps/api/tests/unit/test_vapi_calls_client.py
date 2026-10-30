"""`VapiCallsClient` against `httpx.MockTransport`: no request ever leaves
the process. Pins down the auth header, window splitting on a full page,
de-duplication, the retry policy (429 with and without Retry-After, 5xx,
timeouts), auth failures that are never retried, 404 handling, and that
neither the key nor a response body reaches the logs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from structlog.testing import capture_logs

from app.domain.usage_reconciliation import (
    VapiApiError,
    VapiAuthError,
    VapiPagingError,
    VapiUnavailableError,
)
from app.infrastructure.telephony.vapi_calls_client import VapiCallsClient

_KEY = "test-private-key-0000"
_START = datetime(2026, 10, 20, tzinfo=timezone.utc)
_END = _START + timedelta(days=2)


def _call(call_id: str, created: datetime) -> dict:
    return {"id": call_id, "status": "ended", "createdAt": created.isoformat(),
            "endedReason": "customer-ended-call", "customer": {"number": "+16305550199"},
            "artifact": {"transcript": "private words"}}


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _client(handler, *, page_limit: int = 1000, sleep: _Sleeps | None = None,
            max_attempts: int = 4) -> VapiCallsClient:
    return VapiCallsClient(
        api_key=_KEY, transport=httpx.MockTransport(handler), page_limit=page_limit,
        sleep=sleep or _Sleeps(), jitter=lambda: 0.5, min_request_interval_seconds=0,
        max_attempts=max_attempts,
    )


def _window(request: httpx.Request) -> tuple[datetime, datetime]:
    query = parse_qs(request.url.query.decode())
    parse = lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))  # noqa: E731
    return parse(query["createdAtGe"][0]), parse(query["createdAtLt"][0])


@pytest.mark.asyncio
async def test_lists_with_the_private_key_and_the_documented_filters():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_call("c1", _START)])

    calls = await _client(handler).list_calls(_START, _END)

    assert [call.vapi_call_id for call in calls] == ["c1"]
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url).startswith("https://api.vapi.ai/call?")
    assert request.headers["Authorization"] == f"Bearer {_KEY}"
    query = parse_qs(request.url.query.decode())
    assert query["createdAtGe"] == ["2026-10-20T00:00:00.000Z"]
    assert query["createdAtLt"] == ["2026-10-22T00:00:00.000Z"]
    assert query["limit"] == ["1000"]


@pytest.mark.asyncio
async def test_a_full_page_splits_the_window_until_every_page_is_short():
    """Vapi's list has no cursor: a full page may have dropped calls."""
    created = [_START + timedelta(hours=hour) for hour in range(0, 48, 6)]  # 8 calls

    def handler(request: httpx.Request) -> httpx.Response:
        start, end = _window(request)
        page = [_call(f"c{i}", moment) for i, moment in enumerate(created) if start <= moment < end]
        return httpx.Response(200, json=page[:3])  # what a capped list would return

    calls = await _client(handler, page_limit=3).list_calls(_START, _END)
    assert sorted(call.vapi_call_id for call in calls) == sorted(f"c{i}" for i in range(8))


@pytest.mark.asyncio
async def test_a_call_returned_twice_is_kept_once():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_call("dup", _START), _call("dup", _START)])

    calls = await _client(handler).list_calls(_START, _END)
    assert [call.vapi_call_id for call in calls] == ["dup"]


@pytest.mark.asyncio
async def test_a_full_page_in_an_unsplittable_window_refuses_rather_than_truncates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_call(f"c{i}", _START) for i in range(2)])

    with pytest.raises(VapiPagingError):
        await _client(handler, page_limit=2).list_calls(_START, _START + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_429_honours_retry_after_then_succeeds():
    sleeps = _Sleeps()
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                      httpx.Response(200, json=[])])
    await _client(lambda r: next(responses), sleep=sleeps).list_calls(_START, _END)
    assert sleeps.calls == [7.0]


@pytest.mark.asyncio
async def test_retry_after_is_capped():
    sleeps = _Sleeps()
    responses = iter([httpx.Response(429, headers={"Retry-After": "86400"}),
                      httpx.Response(200, json=[])])
    await _client(lambda r: next(responses), sleep=sleeps).list_calls(_START, _END)
    assert sleeps.calls == [60.0]


@pytest.mark.asyncio
async def test_5xx_and_timeouts_back_off_then_give_up():
    sleeps = _Sleeps()
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] % 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(503)

    with pytest.raises(VapiUnavailableError):
        await _client(handler, sleep=sleeps).list_calls(_START, _END)
    assert attempts["n"] == 4
    # Exponential (jitter pinned at 1.0x): 1s, 2s, 4s.
    assert sleeps.calls == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_auth_failures_are_never_retried(status: int):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(status)

    with pytest.raises(VapiAuthError):
        await _client(handler).list_calls(_START, _END)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_another_4xx_is_an_error_not_a_retry():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"message": "bad"})

    with pytest.raises(VapiApiError):
        await _client(handler).list_calls(_START, _END)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_a_body_that_is_not_a_list_is_refused():
    with pytest.raises(VapiUnavailableError):
        await _client(lambda r: httpx.Response(200, json={"results": []})).list_calls(_START, _END)


@pytest.mark.asyncio
async def test_get_call_returns_none_on_404_and_quotes_the_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    assert await _client(handler).get_call("../assistant/x?y=1") is None
    assert seen[0].url.raw_path == b"/call/..%2Fassistant%2Fx%3Fy%3D1"


@pytest.mark.asyncio
async def test_get_call_parses_the_call():
    summary = await _client(lambda r: httpx.Response(200, json=_call("c9", _START))).get_call("c9")
    assert summary is not None and summary.vapi_call_id == "c9"


@pytest.mark.asyncio
async def test_get_call_refuses_a_different_call_than_asked_for():
    with pytest.raises(VapiUnavailableError):
        await _client(lambda r: httpx.Response(200, json=_call("other", _START))).get_call("c9")


@pytest.mark.asyncio
async def test_neither_the_key_nor_a_body_reaches_the_logs():
    responses = iter([httpx.Response(500, text="boom +16305550199"),
                      httpx.Response(200, json=[_call("c1", _START), {"no": "id"}])])
    with capture_logs() as logs:
        await _client(lambda r: next(responses)).list_calls(_START, _END)
    text = repr(logs)
    assert logs, "expected the retry and the unparseable item to be logged"
    assert _KEY not in text and "5550199" not in text and "private words" not in text


@pytest.mark.parametrize("url", ["http://api.vapi.ai", "https://evil.example", "https://vapi.ai.evil.example"])
def test_the_key_is_only_ever_sent_over_https_to_vapi(url: str):
    with pytest.raises(ValueError):
        VapiCallsClient(api_key=_KEY, base_url=url)


def test_a_blank_key_is_refused():
    with pytest.raises(ValueError):
        VapiCallsClient(api_key="  ")
