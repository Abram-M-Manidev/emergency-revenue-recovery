"""Call usage metering: what one phone call is billed as, and why.

One `CallUsageRecord` per Vapi call, written from the end-of-call report —
including calls ERRS never held a conversation on (the caller hung up before
saying anything, or the assistant was switched off), because Vapi bills
those too. Deliberately separate from `voice_calls`: that row exists only
once a chat-completion turn has happened, and it is call-handling state,
not a billing record.

Every billing decision is made by `decide_billing`, a pure function of the
facts on the record, and stamped with `RULES_VERSION` so a later rule change
never silently re-describes an old month. The rules (agreed 2026-10-29):

- Billing period: the UTC calendar month of the call's start. Falls back to
  its end, then to when ERRS first received the report — the basis used is
  stored beside the period.
- Duration: Vapi's reported duration; else the report's own start/end
  timestamps; else unknown. An unknown duration is never guessed: the call
  bills nothing and is flagged `DURATION_UNKNOWN` for reconciliation.
- Under `MINIMUM_BILLABLE_SECONDS` bills nothing.
- A call that ended on an ESSR/Vapi/telephony fault (`is_fault_ended_reason`)
  bills nothing.
- Everything else bills `ceil(seconds / 60)` minutes, rounded per call.

Nothing here enforces an allowance or changes how a call is handled. This
module only records.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

RULES_VERSION = "m1-2026-10-29"
MINIMUM_BILLABLE_SECONDS = 10


class UsageAttribution(str, Enum):
    """How the record's organization was decided."""

    VOICE_CALL = "voice_call"
    """From the `voice_calls` row ERRS created on the call's first turn."""

    VOICE_LINE = "voice_line"
    """No turn happened; resolved from the report's assistant (or, with no
    assistant id, phone number) through `voice_lines`."""

    UNATTRIBUTED = "unattributed"
    """No mapping matched. Kept, operator-only, because Vapi still bills it —
    never guessed onto a tenant."""


class DurationSource(str, Enum):
    REPORTED = "reported"
    TIMESTAMPS = "timestamps"
    MISSING = "missing"


class PeriodBasis(str, Enum):
    STARTED_AT = "started_at"
    ENDED_AT = "ended_at"
    RECEIVED_AT = "received_at"


class ExclusionReason(str, Enum):
    DURATION_UNKNOWN = "duration_unknown"
    UNDER_MINIMUM = "under_minimum"
    FAULT_ENDED_REASON = "fault_ended_reason"


# Vapi's `endedReason` vocabulary for calls that failed rather than ended.
# Prefixes cover Vapi's families of pipeline/provider errors; the exact set
# covers single values. Not verified against a complete list from Vapi:
# an unrecognised reason is treated as a normal end (billable), and every
# record keeps its raw `ended_reason`, so a missed fault is visible and
# correctable rather than hidden.
_FAULT_PREFIXES = (
    "pipeline-error-",
    "call.in-progress.error-",
    "call.start.error-",
    "call.ringing.error-",
)
_FAULT_REASONS = frozenset(
    {
        "assistant-error",
        "assistant-request-failed",
        "assistant-request-returned-error",
        "assistant-not-found",
        "assistant-not-valid",
        "phone-call-provider-closed-websocket",
        "twilio-failed-to-connect-call",
        "vonage-failed-to-connect-call",
        "vonage-rejected",
    }
)


def is_fault_ended_reason(ended_reason: str | None) -> bool:
    """True when Vapi says the call ended because something broke — ours,
    Vapi's, or a provider's. The Custom-LLM failures (`custom-llm-...`) are
    ERRS's own fault by definition, wherever Vapi nests them."""
    if not ended_reason:
        return False
    reason = ended_reason.strip().lower()
    return (
        reason in _FAULT_REASONS
        or reason.startswith(_FAULT_PREFIXES)
        or "custom-llm" in reason
    )


@dataclass(frozen=True, slots=True)
class CallUsageFacts:
    """What the end-of-call report said, as reported. Every field but the id
    is optional: Vapi's payload shape is not verified, and a missing value
    must stay missing rather than become a zero."""

    vapi_call_id: str
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    reported_duration_seconds: Decimal | None = None
    ended_reason: str | None = None
    # Vapi's own figures, stored exactly as reported and labelled as such.
    # Never computed or estimated here.
    vapi_reported_cost: Decimal | None = None
    vapi_reported_cost_breakdown: dict[str, Decimal] | None = None


@dataclass(frozen=True, slots=True)
class BillingDecision:
    billing_period: date
    period_basis: PeriodBasis
    duration_source: DurationSource
    billable_seconds: int
    billable_minutes: int
    is_billable: bool
    exclusion_reason: ExclusionReason | None
    rules_version: str = RULES_VERSION


def measured_duration_seconds(facts: CallUsageFacts) -> tuple[Decimal | None, DurationSource]:
    if facts.reported_duration_seconds is not None:
        return facts.reported_duration_seconds, DurationSource.REPORTED
    if (
        facts.started_at is not None
        and facts.ended_at is not None
        and facts.ended_at >= facts.started_at
    ):
        seconds = Decimal(str((facts.ended_at - facts.started_at).total_seconds()))
        return seconds, DurationSource.TIMESTAMPS
    return None, DurationSource.MISSING


def billing_period_for(
    facts: CallUsageFacts, *, received_at: datetime
) -> tuple[date, PeriodBasis]:
    for moment, basis in (
        (facts.started_at, PeriodBasis.STARTED_AT),
        (facts.ended_at, PeriodBasis.ENDED_AT),
        (received_at, PeriodBasis.RECEIVED_AT),
    ):
        if moment is not None:
            utc = moment.astimezone(timezone.utc)
            return date(utc.year, utc.month, 1), basis
    raise AssertionError("received_at is always present")  # pragma: no cover


def decide_billing(facts: CallUsageFacts, *, received_at: datetime) -> BillingDecision:
    period, basis = billing_period_for(facts, received_at=received_at)
    seconds, source = measured_duration_seconds(facts)

    if seconds is None:
        return BillingDecision(period, basis, source, 0, 0, False, ExclusionReason.DURATION_UNKNOWN)

    # Fractional seconds round up, so the duration a minute is billed on is
    # never less than the one Vapi reported.
    billable_seconds = math.ceil(seconds)
    if seconds < MINIMUM_BILLABLE_SECONDS:
        return BillingDecision(
            period, basis, source, billable_seconds, 0, False, ExclusionReason.UNDER_MINIMUM
        )
    if is_fault_ended_reason(facts.ended_reason):
        return BillingDecision(
            period, basis, source, billable_seconds, 0, False, ExclusionReason.FAULT_ENDED_REASON
        )
    return BillingDecision(
        period, basis, source, billable_seconds, math.ceil(billable_seconds / 60), True, None
    )


def merge_facts(existing: CallUsageFacts, incoming: CallUsageFacts) -> CallUsageFacts:
    """A repeated report for the same call. A value it carries replaces the
    stored one (a later report is the more complete one); a value it lacks
    never erases what is stored."""
    return replace(
        existing,
        **{
            name: getattr(incoming, name)
            for name in (
                "vapi_assistant_id",
                "vapi_phone_number_id",
                "started_at",
                "ended_at",
                "reported_duration_seconds",
                "ended_reason",
                "vapi_reported_cost",
                "vapi_reported_cost_breakdown",
            )
            if getattr(incoming, name) is not None
        },
    )


@dataclass(frozen=True, slots=True)
class CallUsageRecord:
    id: uuid.UUID
    organization_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    attribution: UsageAttribution
    facts: CallUsageFacts
    decision: BillingDecision
    report_count: int
    first_reported_at: datetime
    last_reported_at: datetime

    @property
    def vapi_call_id(self) -> str:
        return self.facts.vapi_call_id
