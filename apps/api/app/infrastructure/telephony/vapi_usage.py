"""Reads the usage facts out of a Vapi end-of-call report.

The report's cost and timing fields are NOT verified against a real payload
yet (Vapi's server-event docs do not specify them). So every field is
optional and read defensively: a value that is absent, malformed or
implausible becomes None — never a zero, never a guess — and the billing
rules then treat it as unknown. The raw payload is never stored: it carries
the transcript and recording links, which are caller data.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.usage import CallUsageFacts

_ID_MAX_LENGTH = 128
_ENDED_REASON_MAX_LENGTH = 128
# Plausibility bounds, not business rules: a value outside them is a
# malformed report, and recording it would corrupt a month's totals.
_MAX_DURATION_SECONDS = Decimal(48 * 3600)
_MAX_CALL_COST = Decimal(10_000)
_MAX_BREAKDOWN_VALUE = Decimal(10**12)
# The components Vapi's SDK documents on `CostBreakdown`. Anything else is
# dropped rather than stored unvetted.
_BREAKDOWN_KEYS = frozenset(
    {
        "transport",
        "stt",
        "llm",
        "tts",
        "vapi",
        "total",
        "llmPromptTokens",
        "llmCompletionTokens",
        "ttsCharacters",
    }
)


def parse_end_of_call_usage(message: dict[str, Any], vapi_call_id: str) -> CallUsageFacts:
    call = message.get("call")
    call = call if isinstance(call, dict) else {}
    assistant = message.get("assistant")
    assistant = assistant if isinstance(assistant, dict) else {}
    phone_number = message.get("phoneNumber")
    phone_number = phone_number if isinstance(phone_number, dict) else {}

    return CallUsageFacts(
        vapi_call_id=vapi_call_id,
        vapi_assistant_id=_identifier(call.get("assistantId") or assistant.get("id")),
        vapi_phone_number_id=_identifier(call.get("phoneNumberId") or phone_number.get("id")),
        started_at=_timestamp(_first(message, call, "startedAt")),
        ended_at=_timestamp(_first(message, call, "endedAt")),
        reported_duration_seconds=_duration(message),
        ended_reason=_text(message.get("endedReason") or call.get("endedReason")),
        vapi_reported_cost=_amount(_first(message, call, "cost"), _MAX_CALL_COST),
        vapi_reported_cost_breakdown=_breakdown(_first(message, call, "costBreakdown")),
    )


def _first(message: dict[str, Any], call: dict[str, Any], key: str) -> Any:
    value = message.get(key)
    return value if value is not None else call.get(key)


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > _ID_MAX_LENGTH:
        return None
    return value.strip()


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_ENDED_REASON_MAX_LENGTH]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp's zone is unknown; assuming one would shift calls
    # across a month boundary.
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _amount(value: Any, maximum: Decimal) -> Decimal | None:
    number = _decimal(value)
    if number is None or number < 0 or number > maximum:
        return None
    return number


def _duration(message: dict[str, Any]) -> Decimal | None:
    seconds = message.get("durationSeconds")
    if seconds is None:
        seconds = message.get("duration")
    parsed = _amount(seconds, _MAX_DURATION_SECONDS)
    if parsed is not None:
        return parsed
    milliseconds = _amount(message.get("durationMs"), _MAX_DURATION_SECONDS * 1000)
    return None if milliseconds is None else milliseconds / 1000


def _breakdown(value: Any) -> dict[str, Decimal] | None:
    if not isinstance(value, dict):
        return None
    kept: dict[str, Decimal] = {}
    for key in sorted(_BREAKDOWN_KEYS & value.keys()):
        # Bounded by form, not by a currency limit: some components are
        # token and character counts.
        number = _amount(value[key], _MAX_BREAKDOWN_VALUE)
        if number is not None:
            kept[key] = number
    return kept or None
