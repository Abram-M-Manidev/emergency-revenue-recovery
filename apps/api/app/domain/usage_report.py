"""Usage reporting (M3) and usage-data health (M4).

M3 reads what M1 recorded and M2 reconciled, per organization and billing
period. It is a report, not an invoice: nothing here charges anyone or
changes a record, and the numbers are exactly the stored `billable_minutes`
decided under each record's own `rules_version`.

M4 decides, from a handful of counts, whether the usage data can be trusted
right now: has reconciliation run recently and succeeded, are calls ending
without a usage record, are records arriving that no organization owns.
`evaluate_usage_health` is pure so its thresholds are tested exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass(frozen=True, slots=True)
class ModelTokenUsage:
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class UsageSummary:
    billing_period: date
    calls: int
    billable_calls: int
    billable_minutes: int
    #: Calls recorded but not billed, by `ExclusionReason` value.
    excluded_calls: dict[str, int]
    #: Calls reconciled against Vapi, by `ReconciliationOutcome` value.
    reconciliation: dict[str, int]
    #: Calls with no reconciliation result yet.
    unreconciled_calls: int
    rules_versions: list[str]
    llm_usage: list[ModelTokenUsage] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UsageHealthFacts:
    now: datetime
    last_run_started_at: datetime | None
    last_run_status: str | None
    last_success_finished_at: datetime | None
    #: Ended calls in the lookback window with no `call_usage_records` row.
    calls_missing_usage: int
    #: Usage records in the lookback window that no organization owns.
    unattributed_records: int
    #: Reconciled calls in the lookback window whose outcome is not clean.
    discrepant_calls: int


@dataclass(frozen=True, slots=True)
class UsageHealth:
    healthy: bool
    problems: list[str]


#: Reconciliation is expected daily; a little over a day allows for drift.
DEFAULT_MAX_RECONCILIATION_AGE = timedelta(hours=26)


def evaluate_usage_health(
    facts: UsageHealthFacts, *, max_reconciliation_age: timedelta = DEFAULT_MAX_RECONCILIATION_AGE
) -> UsageHealth:
    """Problem codes, each one something an operator must act on. Codes only:
    they go to logs and alerts, so they carry no caller or tenant data."""
    problems: list[str] = []
    if facts.last_run_started_at is None:
        problems.append("reconciliation_never_run")
    elif facts.last_run_status == "failed":
        problems.append("reconciliation_last_run_failed")
    if facts.last_run_started_at is not None and (
        facts.last_success_finished_at is None
        or facts.now - facts.last_success_finished_at > max_reconciliation_age
    ):
        problems.append("reconciliation_stale")
    if facts.calls_missing_usage:
        problems.append("calls_missing_usage")
    if facts.unattributed_records:
        problems.append("unattributed_usage")
    if facts.discrepant_calls:
        problems.append("reconciliation_discrepancies")
    return UsageHealth(healthy=not problems, problems=problems)
