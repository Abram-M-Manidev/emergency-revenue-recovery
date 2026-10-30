"""Usage reconciliation (M2): checking `call_usage_records` against what
Vapi's own call API says, after the fact.

The end-of-call webhook is the primary source (see `app/domain/usage.py`),
but a webhook can be lost, can arrive without fields, and Vapi's cost can
settle after the report was sent. Reconciliation reads Vapi's view of each
call and stores it NEXT TO ours, never on top of it:

- A call Vapi has and we do not is recorded (`UsageSource.RECONCILIATION`)
  through the same attribution and billing rules a report would use.
- A field we lack is filled from Vapi. A field we have is never
  overwritten: a disagreement is recorded as a discrepancy for an
  operator, not resolved.
- The billing decision is re-made only for a record whose duration was
  unknown (M1 flags those "for reconciliation"), and even then its billing
  period and attribution stay as they were.

Every rule here is a pure function. Nothing in this module bills anyone.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.domain.usage import (
    CallUsageFacts,
    CallUsageRecord,
    ExclusionReason,
    UsageSource,
    decide_billing,
    is_fault_ended_reason,
    measured_duration_seconds,
)

# Tolerances for "the same value". Duration: Vapi's timestamps and ours can
# legitimately differ by rounding. Cost: Vapi reports USD as a double.
DURATION_TOLERANCE_SECONDS = Decimal(1)
COST_TOLERANCE = Decimal("0.0001")

VAPI_STATUS_ENDED = "ended"


@dataclass(frozen=True, slots=True)
class VapiCallSummary:
    """The usage-relevant part of one Vapi `Call` object, as Vapi reported
    it. Everything else (transcript, customer number, recording) is
    discarded by the parser before this exists."""

    vapi_call_id: str
    status: str | None = None
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    ended_reason: str | None = None
    cost: Decimal | None = None
    cost_breakdown: dict[str, Decimal] | None = None
    # Summed from the `costs[]` line items. None when Vapi sent no such item.
    vapi_minutes_normal: Decimal | None = None
    vapi_minutes_overage: Decimal | None = None
    transport_minutes: Decimal | None = None
    # True when any `ModelCost` item says `usageComplete: false`, i.e. Vapi
    # itself says the cost may be understated.
    usage_incomplete: bool = False

    @property
    def is_ended(self) -> bool:
        return self.status == VAPI_STATUS_ENDED

    @property
    def duration_seconds(self) -> Decimal | None:
        """Vapi's `Call` has no duration field: it is `endedAt − startedAt`."""
        if self.started_at is None or self.ended_at is None or self.ended_at < self.started_at:
            return None
        return Decimal(str((self.ended_at - self.started_at).total_seconds()))

    def to_facts(self) -> CallUsageFacts:
        return CallUsageFacts(
            vapi_call_id=self.vapi_call_id,
            vapi_assistant_id=self.vapi_assistant_id,
            vapi_phone_number_id=self.vapi_phone_number_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            ended_reason=self.ended_reason,
            vapi_reported_cost=self.cost,
            vapi_reported_cost_breakdown=self.cost_breakdown,
        )


class ReconciliationOutcome(str, Enum):
    MATCHED = "matched"
    """Ours and Vapi's agree on everything both have."""

    FILLED = "filled"
    """We lacked fields Vapi had; they were filled, and nothing disagrees."""

    DISCREPANT = "discrepant"
    """At least one value disagrees — see the discrepancies."""

    MISSING_LOCALLY = "missing_locally"
    """Vapi has the call; no end-of-call report ever reached us. Recorded
    from Vapi's data. Stays this outcome until a report arrives."""

    MISSING_AT_VAPI = "missing_at_vapi"
    """We have the call; Vapi returned 404 for it (deleted, or past Vapi's
    retention)."""

    NOT_ENDED = "not_ended"
    """Vapi does not report the call as ended yet. Nothing was changed; the
    next run checks it again."""


class Discrepancy(str, Enum):
    DURATION = "duration"
    ENDED_REASON = "ended_reason"
    COST = "cost"
    FAULT_CLASSIFICATION = "fault_classification"
    """Vapi's `endedReason` is a failure under the current rules, yet the
    record bills minutes (decided under older rules, or on a different
    reason)."""

    COST_INCOMPLETE = "cost_incomplete"
    """Vapi says its own cost may be understated (`usageComplete: false`)."""


# The facts fields reconciliation may fill when ours is NULL. Never
# `reported_duration_seconds`: Vapi's call API has no duration field, so the
# duration comes from the filled timestamps instead (`DurationSource.TIMESTAMPS`).
_FILLABLE = (
    "vapi_assistant_id",
    "vapi_phone_number_id",
    "started_at",
    "ended_at",
    "ended_reason",
    "vapi_reported_cost",
    "vapi_reported_cost_breakdown",
)


def fill_missing(record: CallUsageRecord, vapi: VapiCallSummary) -> tuple[CallUsageRecord, list[str]]:
    """Fills only what the record lacks; returns the record and the names of
    the fields filled. Never changes a value we have, the attribution, the
    report counters or the billing period."""
    incoming = vapi.to_facts()
    filled = [
        name
        for name in _FILLABLE
        if getattr(record.facts, name) is None and getattr(incoming, name) is not None
    ]
    if not filled:
        return record, []
    facts = replace(record.facts, **{name: getattr(incoming, name) for name in filled})
    decision = record.decision
    if decision.exclusion_reason is ExclusionReason.DURATION_UNKNOWN:
        # The one decision M1 left open for reconciliation. Re-made under the
        # current rules (and stamped with them), but the period stays where
        # the record already counted it.
        redecided = decide_billing(facts, received_at=record.first_reported_at)
        decision = replace(
            redecided, billing_period=decision.billing_period, period_basis=decision.period_basis
        )
    return replace(record, facts=facts, decision=decision), filled


def find_discrepancies(record: CallUsageRecord, vapi: VapiCallSummary) -> list[Discrepancy]:
    """Disagreements between values BOTH sides have. A value only one side
    has is a gap (see `fill_missing`), not a discrepancy."""
    found: list[Discrepancy] = []
    ours, _ = measured_duration_seconds(record.facts)
    theirs = vapi.duration_seconds
    if ours is not None and theirs is not None and abs(ours - theirs) > DURATION_TOLERANCE_SECONDS:
        found.append(Discrepancy.DURATION)
    if (
        record.facts.ended_reason is not None
        and vapi.ended_reason is not None
        and record.facts.ended_reason != vapi.ended_reason
    ):
        found.append(Discrepancy.ENDED_REASON)
    if (
        record.facts.vapi_reported_cost is not None
        and vapi.cost is not None
        and abs(record.facts.vapi_reported_cost - vapi.cost) > COST_TOLERANCE
    ):
        found.append(Discrepancy.COST)
    if record.decision.is_billable and is_fault_ended_reason(vapi.ended_reason):
        found.append(Discrepancy.FAULT_CLASSIFICATION)
    if vapi.usage_incomplete:
        found.append(Discrepancy.COST_INCOMPLETE)
    return found


def classify(
    record: CallUsageRecord, vapi: VapiCallSummary, *, filled: list[str]
) -> tuple[ReconciliationOutcome, list[Discrepancy]]:
    """The outcome for an ended call both sides have (after any fill)."""
    discrepancies = find_discrepancies(record, vapi)
    if record.source is UsageSource.RECONCILIATION and record.report_count == 0:
        return ReconciliationOutcome.MISSING_LOCALLY, discrepancies
    if discrepancies:
        return ReconciliationOutcome.DISCREPANT, discrepancies
    if filled:
        return ReconciliationOutcome.FILLED, discrepancies
    return ReconciliationOutcome.MATCHED, discrepancies


class ReconciliationRunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReconciliationRun:
    id: uuid.UUID
    window_start: datetime
    window_end: datetime
    started_at: datetime
    status: ReconciliationRunStatus
    finished_at: datetime | None = None
    # {outcome value: count}, plus "created", "filled", "errors".
    counts: dict[str, int] | None = None
    # An exception TYPE name only — never a message, which can quote data.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CallReconciliation:
    """The latest reconciliation of one Vapi call."""

    vapi_call_id: str
    run_id: uuid.UUID
    outcome: ReconciliationOutcome
    discrepancies: list[Discrepancy]
    filled_fields: list[str]
    checked_at: datetime
    call_usage_record_id: uuid.UUID | None = None
    # None when Vapi had nothing to say (MISSING_AT_VAPI): the stored
    # snapshot from an earlier run is then kept, not erased.
    vapi: VapiCallSummary | None = None


# --- errors -----------------------------------------------------------------


class VapiApiError(Exception):
    """Vapi's API could not give an answer. Raised to abort a run: a partial
    view of Vapi would misreport every call it missed."""


class VapiAuthError(VapiApiError):
    """401/403: the API key is missing, wrong or not permitted. Never
    retried."""


class VapiUnavailableError(VapiApiError):
    """Timeouts, 429s or 5xx past the retry budget, or a malformed body."""


class VapiPagingError(VapiApiError):
    """A window too narrow to split still returned a full page: the listing
    cannot be proven complete, so the run refuses to pretend it is."""


class ReconciliationAlreadyRunningError(Exception):
    """Another run holds the reconciliation lock."""


# --- ports ------------------------------------------------------------------


class VapiCallSource(ABC):
    @abstractmethod
    async def list_calls(self, start: datetime, end: datetime) -> list[VapiCallSummary]:
        """Every call Vapi created in [start, end), each once. Raises
        `VapiApiError` rather than return an incomplete list."""
        ...

    @abstractmethod
    async def get_call(self, vapi_call_id: str) -> VapiCallSummary | None:
        """One call, or None when Vapi has no such call (404)."""
        ...
