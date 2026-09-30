"""Concrete `PagingProvider` adapters.

- `NullPagingProvider` — the default. Reports every page NOT_CONFIGURED, so
  nothing ever claims a technician was paged.
- `LoggingPagingProvider` — development only. Reports ACCEPTED while paging
  nobody; `Settings` refuses to boot with it in production.
- `TwilioPagingProvider` — SMS through Twilio's Messages API and automated
  voice through its Calls API, over plain HTTPS (no SDK, so nothing
  vendor-specific leaks past this module).

None of them raises, and none logs a phone number or a message body.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

import httpx
import structlog

from app.core.config import Settings
from app.domain.paging.page import PageMessage, PagingOutcome, PagingReceipt
from app.domain.paging.port import PagingProvider
from app.domain.paging.settings import PagingChannel

logger = structlog.get_logger("app.paging.providers")

_TWILIO_API = "https://api.twilio.com/2010-04-01"


class NullPagingProvider(PagingProvider):
    @property
    def name(self) -> str:
        return "none"

    def supports(self, channel: PagingChannel) -> bool:
        # Claims every channel so the refusal is recorded as "no provider"
        # on the notification, rather than silently skipped.
        return True

    async def send(self, message: PageMessage) -> PagingReceipt:
        return PagingReceipt(
            PagingOutcome.NOT_CONFIGURED, self.name, error_code="no_provider_configured"
        )


class LoggingPagingProvider(PagingProvider):
    """Writes that a page would have been sent, and calls it accepted.

    Identifiers only: the destination is a personal mobile and the body
    carries the caller's details."""

    @property
    def name(self) -> str:
        return "logging"

    def supports(self, channel: PagingChannel) -> bool:
        return True

    async def send(self, message: PageMessage) -> PagingReceipt:
        logger.warning(
            "emergency_page_logged_not_sent",
            organization_id=str(message.organization_id),
            ticket_id=str(message.ticket_id),
            channel=message.channel.value,
            idempotency_key=message.idempotency_key,
            note="development provider — nobody was actually paged",
        )
        return PagingReceipt(PagingOutcome.ACCEPTED, self.name)


class TwilioPagingProvider(PagingProvider):
    """SMS: `POST /Accounts/{sid}/Messages.json` (To, From, Body).
    Voice: `POST /Accounts/{sid}/Calls.json` (To, From, Twiml with `<Say>`).

    A 2xx means Twilio queued the message or call — `ACCEPTED`, which is all
    it means: not delivered, not answered, not acknowledged. 4xx other than
    429 is a permanent refusal (invalid number, unverified destination, bad
    credentials) and is not retried; 429, 5xx, timeouts and network errors
    are transient.

    Twilio does not deduplicate API requests, so the idempotency key cannot
    be enforced by the provider; it is recorded on our side only."""

    def __init__(
        self,
        *,
        account_sid: str | None,
        auth_token: str | None,
        from_number: str | None,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account_sid = (account_sid or "").strip()
        self._auth_token = (auth_token or "").strip()
        self._from = (from_number or "").strip()
        self._timeout = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return "twilio"

    def supports(self, channel: PagingChannel) -> bool:
        return channel in (PagingChannel.SMS, PagingChannel.VOICE)

    async def send(self, message: PageMessage) -> PagingReceipt:
        if not (self._account_sid and self._auth_token and self._from):
            return PagingReceipt(
                PagingOutcome.NOT_CONFIGURED, self.name, error_code="credentials_missing"
            )
        if message.channel is PagingChannel.SMS:
            resource = "Messages.json"
            form = {"To": message.to, "From": self._from, "Body": message.body}
        else:
            resource = "Calls.json"
            spoken = escape(message.body)
            form = {
                "To": message.to,
                "From": self._from,
                # Said twice: the first words of a call are often lost while
                # the recipient is still bringing the phone to their ear.
                "Twiml": (
                    f'<Response><Say>{spoken}</Say><Pause length="1"/>'
                    f"<Say>{spoken}</Say></Response>"
                ),
            }
        url = f"{_TWILIO_API}/Accounts/{self._account_sid}/{resource}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    url, data=form, auth=(self._account_sid, self._auth_token)
                )
        except httpx.TimeoutException:
            return PagingReceipt(PagingOutcome.FAILED, self.name, error_code="timeout")
        except httpx.HTTPError as exc:
            # Type only — httpx messages include the URL (with the account
            # SID) and never help more than the type does.
            logger.warning("emergency_page_transport_error", error=type(exc).__name__)
            return PagingReceipt(PagingOutcome.FAILED, self.name, error_code="transport_error")

        if 200 <= response.status_code < 300:
            return PagingReceipt(
                PagingOutcome.ACCEPTED, self.name, provider_message_id=_sid(response)
            )
        error_code = f"http_{response.status_code}"
        twilio_code = _twilio_error_code(response)
        if twilio_code is not None:
            error_code = f"twilio_{twilio_code}"
        if response.status_code == 429 or response.status_code >= 500:
            return PagingReceipt(PagingOutcome.FAILED, self.name, error_code=error_code)
        return PagingReceipt(PagingOutcome.REJECTED, self.name, error_code=error_code)


def _sid(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    sid = payload.get("sid") if isinstance(payload, dict) else None
    return sid if isinstance(sid, str) and sid.isalnum() else None


def _twilio_error_code(response: httpx.Response) -> int | None:
    """Twilio's numeric error code (e.g. 21211 invalid 'To' number). Only the
    number is kept — the error body's message can quote the destination."""
    try:
        payload = response.json()
    except ValueError:
        return None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, int) else None


def build_paging_provider(settings: Settings) -> PagingProvider:
    """Which adapter sends pages, from configuration. `none` by default."""
    if settings.PAGING_PROVIDER == "twilio":
        return TwilioPagingProvider(
            account_sid=settings.TWILIO_ACCOUNT_SID,
            auth_token=settings.TWILIO_AUTH_TOKEN,
            from_number=settings.TWILIO_PHONE_NUMBER,
            timeout_seconds=settings.PAGING_SEND_TIMEOUT_SECONDS,
        )
    if settings.PAGING_PROVIDER == "logging":
        return LoggingPagingProvider()
    return NullPagingProvider()
