"""Moving a live Vapi call to a person, through Vapi Live Call Control.

    POST {call.monitor.controlUrl}
    {"type": "transfer",
     "destination": {"type": "number", "number": "+15551234567"},
     "content": "I'm connecting you with someone who can help now."}

Why this rather than emitting Vapi's `transferCall` tool from the Custom-LLM
stream (the way `endCall` is emitted): the control request gives the backend
a real answer. A 2xx means Vapi accepted the transfer, so the assistant may
say so; anything else means it did not, and the caller is told the truth.
A tool call streamed back to Vapi is fire-and-forget — we would learn it
failed only after the caller had already been told they were being
connected.

`content` is spoken by Vapi as part of the accepted transfer, so "connecting
you" is only ever heard for a transfer Vapi took.

Requires `assistant.monitorPlan.controlEnabled = true` on the Vapi assistant;
without it Vapi sends no control URL and every transfer is reported
unavailable — honest degradation, never a false "connecting you".
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import structlog

from app.domain.call_transfer.attempt import TransferFailure
from app.domain.call_transfer.port import CallTransferPort, TransferInitiation

logger = structlog.get_logger("app.telephony.vapi_call_control")

# Kept well under AI_TOOL_TIMEOUT_SECONDS (10s) so the tool reports an
# honest PROVIDER_TIMEOUT instead of being cut off by the generic timeout —
# which would leave the attempt recorded as resolved-but-unknown.
_TIMEOUT_SECONDS = 5.0


def is_vapi_control_url(url: str | None) -> bool:
    """A control handle this adapter is willing to POST to.

    The URL arrives inside a request already authenticated by
    `x-vapi-secret`, but it is still attacker-shaped input from the server's
    point of view: an unchecked URL here is a server-side request primitive.
    Only https URLs on Vapi's own domain are used."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "vapi.ai" or host.endswith(".vapi.ai"))


class VapiCallControlTransfer(CallTransferPort):
    def __init__(self, *, timeout_seconds: float = _TIMEOUT_SECONDS,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout_seconds
        self._transport = transport

    async def transfer(
        self, *, call_control: str, destination_number: str, announcement: str
    ) -> TransferInitiation:
        if not is_vapi_control_url(call_control):
            return TransferInitiation(False, TransferFailure.CALL_CONTROL_UNAVAILABLE)
        body = {
            "type": "transfer",
            "destination": {"type": "number", "number": destination_number},
            "content": announcement,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(call_control, json=body)
        except httpx.TimeoutException:
            logger.warning("call_transfer_provider_timeout")
            return TransferInitiation(False, TransferFailure.PROVIDER_TIMEOUT)
        except httpx.HTTPError as exc:
            # Exception type only: the message can contain the URL, and the
            # URL is a capability over the live call.
            logger.warning("call_transfer_provider_error", error=type(exc).__name__)
            return TransferInitiation(False, TransferFailure.PROVIDER_ERROR)

        if 200 <= response.status_code < 300:
            return TransferInitiation(True)
        logger.warning("call_transfer_provider_rejected", status_code=response.status_code)
        return TransferInitiation(False, TransferFailure.PROVIDER_REJECTED)
