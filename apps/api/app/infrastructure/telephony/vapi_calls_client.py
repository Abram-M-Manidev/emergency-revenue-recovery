"""Read-only access to Vapi's call API, for usage reconciliation (M2).

    GET {base}/call?createdAtGe=..&createdAtLt=..&limit=1000   -> [Call, ...]
    GET {base}/call/{id}                                        -> Call | 404
    Authorization: Bearer <VAPI_API_KEY>   (Vapi's PRIVATE key)

Both shapes were checked on 2026-10-30 against Vapi's OpenAPI spec
(https://api.vapi.ai/api-json). Facts this module is built around:

- `GET /call` returns a plain array: no cursor, no page number, and no
  documented sort order. `limit` defaults to 100 and is capped at 1000. So
  a page that comes back FULL may have dropped calls. The window is then
  split in half and each half listed again, until every page is short.
  Calls are de-duplicated by id. A window that cannot be split any further
  and is still full raises `VapiPagingError`, rather than returning a list
  that might be incomplete.
- No rate limit is documented for these endpoints. Requests are therefore
  sent one at a time with a minimum gap, and a 429 is retried after its
  `Retry-After` (or a jittered backoff when it has none). 5xx responses and
  timeouts are retried the same way. 401 and 403 are never retried.

Nothing here writes to Vapi. The key is sent only over https to Vapi's own
domain and is never logged; nor is any response body, which carries
transcripts and caller numbers.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from app.domain.usage_reconciliation import (
    VapiApiError,
    VapiAuthError,
    VapiCallSource,
    VapiCallSummary,
    VapiPagingError,
    VapiUnavailableError,
)
from app.infrastructure.telephony.vapi_call_control import is_vapi_control_url
from app.infrastructure.telephony.vapi_usage import parse_vapi_call

logger = structlog.get_logger("app.telephony.vapi_calls")

DEFAULT_BASE_URL = "https://api.vapi.ai"
PAGE_LIMIT = 1000  # Vapi's documented maximum.
_MIN_WINDOW = timedelta(seconds=1)
_TIMEOUT_SECONDS = 20.0
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
_RETRY_AFTER_CAP_SECONDS = 60.0
_MIN_REQUEST_INTERVAL_SECONDS = 0.25


def _iso(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class VapiCallsClient(VapiCallSource):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        page_limit: int = PAGE_LIMIT,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        max_attempts: int = _MAX_ATTEMPTS,
        min_request_interval_seconds: float = _MIN_REQUEST_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("A Vapi private API key is required.")
        # The key goes in every request: only https, only Vapi's own domain.
        if not is_vapi_control_url(base_url):
            raise ValueError("The Vapi API base URL must be https on vapi.ai.")
        self._headers = {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}
        self._base_url = base_url.rstrip("/")
        self._page_limit = page_limit
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._min_interval = min_request_interval_seconds
        self._transport = transport
        self._sleep = sleep
        self._jitter = jitter
        self._last_request: float | None = None

    async def list_calls(self, start: datetime, end: datetime) -> list[VapiCallSummary]:
        calls: dict[str, VapiCallSummary] = {}
        unparseable = 0
        windows = [(start, end)]
        while windows:
            window_start, window_end = windows.pop()
            page = await self._request(
                "/call",
                params={
                    "createdAtGe": _iso(window_start),
                    "createdAtLt": _iso(window_end),
                    "limit": self._page_limit,
                },
            )
            if not isinstance(page, list):
                raise VapiUnavailableError("GET /call did not return a list")
            if len(page) >= self._page_limit:
                if window_end - window_start <= _MIN_WINDOW:
                    raise VapiPagingError("a one-second window still returned a full page")
                middle = window_start + (window_end - window_start) / 2
                windows.extend([(window_start, middle), (middle, window_end)])
                logger.info("vapi_call_list_window_split", calls_on_page=len(page))
                continue
            for item in page:
                summary = parse_vapi_call(item)
                if summary is None:
                    unparseable += 1
                else:
                    calls[summary.vapi_call_id] = summary
        if unparseable:
            logger.warning("vapi_call_list_items_unparseable", count=unparseable)
        return list(calls.values())

    async def get_call(self, vapi_call_id: str) -> VapiCallSummary | None:
        # Quoted: the id came from a webhook body, and must not be able to
        # steer an authenticated request to another Vapi path.
        body = await self._request(f"/call/{quote(vapi_call_id, safe='')}", not_found_is_none=True)
        if body is None:
            return None
        summary = parse_vapi_call(body)
        if summary is None or summary.vapi_call_id != vapi_call_id:
            raise VapiUnavailableError("GET /call/{id} returned an unusable call")
        return summary

    async def _request(
        self, path: str, *, params: dict[str, Any] | None = None, not_found_is_none: bool = False
    ) -> Any:
        endpoint = "/call" if path == "/call" else "/call/{id}"
        for attempt in range(1, self._max_attempts + 1):
            await self._pace()
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport, follow_redirects=False
                ) as client:
                    response = await client.get(
                        f"{self._base_url}{path}", params=params, headers=self._headers
                    )
            except httpx.TransportError as exc:  # timeouts included
                failure, retry_after = type(exc).__name__, None
            else:
                status = response.status_code
                if status in (401, 403):
                    logger.error("vapi_api_auth_failed", endpoint=endpoint, status=status)
                    raise VapiAuthError(f"Vapi rejected the API key ({status})")
                if status == 404 and not_found_is_none:
                    return None
                if 200 <= status < 300:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise VapiUnavailableError(f"{endpoint} returned non-JSON") from exc
                if status != 429 and status < 500:
                    raise VapiApiError(f"{endpoint} returned {status}")
                failure, retry_after = str(status), _retry_after(response)
            if attempt == self._max_attempts:
                logger.error("vapi_api_gave_up", endpoint=endpoint, failure=failure,
                             attempts=attempt)
                raise VapiUnavailableError(f"{endpoint} failed {attempt} times ({failure})")
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            logger.warning("vapi_api_retry", endpoint=endpoint, failure=failure, attempt=attempt,
                           delay_seconds=round(delay, 2))
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        base = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        return base * (0.5 + self._jitter())

    async def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request is not None:
            wait = self._last_request + self._min_interval - now
            if wait > 0:
                await self._sleep(wait)
        self._last_request = time.monotonic()


def _retry_after(response: httpx.Response) -> float | None:
    """`Retry-After` in seconds (the HTTP-date form is not honoured: the
    jittered backoff covers it). Capped, so a hostile or broken header
    cannot park the run indefinitely."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:  # NaN or negative
        return None
    return min(seconds, _RETRY_AFTER_CAP_SECONDS)
