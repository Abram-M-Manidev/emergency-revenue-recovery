"""Reads the usage facts out of a Vapi end-of-call report, and (M2) the
usage-relevant part of a `Call` object from Vapi's call API.

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
from app.domain.usage_reconciliation import VapiCallSummary

_ID_MAX_LENGTH = 128
_ENDED_REASON_MAX_LENGTH = 128
# Plausibility bounds, not business rules: a value outside them is a
# malformed report, and recording it would corrupt a month's totals.
_MAX_DURATION_SECONDS = Decimal(48 * 3600)
_MAX_CALL_COST = Decimal(10_000)
_MAX_BREAKDOWN_VALUE = Decimal(10**12)
_STATUS_MAX_LENGTH = 32
_MAX_COST_MINUTES = Decimal(48 * 60)
# The numeric components of `CostBreakdown` in Vapi's OpenAPI spec (checked
# 2026-10-30). Anything else, including the nested `analysisCostBreakdown`,
# is dropped rather than stored unvetted.
_BREAKDOWN_KEYS = frozenset(
    {
        "transport",
        "stt",
        "llm",
        "tts",
        "vapi",
        "chat",
        "total",
        "llmPromptTokens",
        "llmCompletionTokens",
        "llmCachedPromptTokens",
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


def parse_vapi_call(call: Any) -> VapiCallSummary | None:
    """One element of `GET /call`, or the body of `GET /call/{id}`, reduced
    to what reconciliation compares. Field names are from Vapi's OpenAPI
    spec, which (unlike the end-of-call report) documents them: `Call` has
    no duration field, and `costs[]` holds typed line items. None when the
    object has no usable id."""
    if not isinstance(call, dict):
        return None
    vapi_call_id = _identifier(call.get("id"))
    if vapi_call_id is None:
        return None
    normal, overage, transport, incomplete = _line_items(call.get("costs"))
    return VapiCallSummary(
        vapi_call_id=vapi_call_id,
        status=_text(call.get("status"), _STATUS_MAX_LENGTH),
        vapi_assistant_id=_identifier(call.get("assistantId")),
        vapi_phone_number_id=_identifier(call.get("phoneNumberId")),
        created_at=_timestamp(call.get("createdAt")),
        updated_at=_timestamp(call.get("updatedAt")),
        started_at=_timestamp(call.get("startedAt")),
        ended_at=_timestamp(call.get("endedAt")),
        ended_reason=_text(call.get("endedReason")),
        cost=_amount(call.get("cost"), _MAX_CALL_COST),
        cost_breakdown=_breakdown(call.get("costBreakdown")),
        vapi_minutes_normal=normal,
        vapi_minutes_overage=overage,
        transport_minutes=transport,
        usage_incomplete=incomplete,
    )


def _line_items(
    costs: Any,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, bool]:
    """Sums `VapiCost.minutes` by `subType` and `TransportCost.minutes`, and
    notes any `ModelCost.usageComplete: false`. A malformed item is skipped;
    a total stays None unless at least one valid item contributed to it."""
    totals: dict[str, Decimal] = {}
    incomplete = False
    for item in costs if isinstance(costs, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "model" and item.get("usageComplete") is False:
            incomplete = True
            continue
        if kind == "vapi":
            key = item.get("subType")
            if key not in ("normal", "overage"):
                continue
        elif kind == "transport":
            key = "transport"
        else:
            continue
        minutes = _amount(item.get("minutes"), _MAX_COST_MINUTES)
        if minutes is not None:
            totals[key] = totals.get(key, Decimal(0)) + minutes
    return totals.get("normal"), totals.get("overage"), totals.get("transport"), incomplete


def _first(message: dict[str, Any], call: dict[str, Any], key: str) -> Any:
    value = message.get(key)
    return value if value is not None else call.get(key)


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > _ID_MAX_LENGTH:
        return None
    return value.strip()


def _text(value: Any, max_length: int = _ENDED_REASON_MAX_LENGTH) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:max_length]


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
