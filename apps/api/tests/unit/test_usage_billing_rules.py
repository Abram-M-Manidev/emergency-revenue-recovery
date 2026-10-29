"""The M1 billing rules (`app/domain/usage.py`) and the defensive reading of
Vapi's end-of-call report (`app/infrastructure/telephony/vapi_usage.py`).

Pure functions, so every agreed rule is pinned here exactly, including the
edges: the 10-second minimum, per-call rounding, UTC month boundaries, and
what happens when a value is missing (it stays missing)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.usage import (
    MINIMUM_BILLABLE_SECONDS,
    RULES_VERSION,
    CallUsageFacts,
    DurationSource,
    ExclusionReason,
    PeriodBasis,
    decide_billing,
    is_fault_ended_reason,
    merge_facts,
)
from app.infrastructure.telephony.vapi_usage import parse_end_of_call_usage

_RECEIVED = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _facts(**overrides) -> CallUsageFacts:
    return CallUsageFacts(vapi_call_id="call_rules", **overrides)


# --- duration and rounding ---------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [("10", 1), ("59.2", 1), ("60", 1), ("60.4", 2), ("61", 2), ("125", 3), ("3600", 60)],
)
def test_billable_minutes_round_up_per_call(seconds: str, minutes: int):
    decision = decide_billing(
        _facts(reported_duration_seconds=Decimal(seconds), ended_reason="customer-ended-call"),
        received_at=_RECEIVED,
    )
    assert decision.is_billable is True
    assert decision.billable_minutes == minutes
    assert decision.exclusion_reason is None
    assert decision.duration_source is DurationSource.REPORTED
    assert decision.rules_version == RULES_VERSION


def test_fractional_seconds_round_up_never_down():
    decision = decide_billing(_facts(reported_duration_seconds=Decimal("60.001")), received_at=_RECEIVED)
    assert decision.billable_seconds == 61
    assert decision.billable_minutes == 2


@pytest.mark.parametrize("seconds", ["0", "1", "9.999"])
def test_calls_under_the_minimum_bill_nothing(seconds: str):
    decision = decide_billing(_facts(reported_duration_seconds=Decimal(seconds)), received_at=_RECEIVED)
    assert Decimal(seconds) < MINIMUM_BILLABLE_SECONDS
    assert decision.is_billable is False
    assert decision.billable_minutes == 0
    assert decision.exclusion_reason is ExclusionReason.UNDER_MINIMUM


def test_exactly_the_minimum_is_billable():
    decision = decide_billing(
        _facts(reported_duration_seconds=Decimal(MINIMUM_BILLABLE_SECONDS)), received_at=_RECEIVED
    )
    assert decision.is_billable is True
    assert decision.billable_minutes == 1


def test_a_missing_duration_is_never_guessed():
    decision = decide_billing(_facts(ended_reason="customer-ended-call"), received_at=_RECEIVED)
    assert decision.is_billable is False
    assert decision.billable_seconds == 0
    assert decision.billable_minutes == 0
    assert decision.duration_source is DurationSource.MISSING
    assert decision.exclusion_reason is ExclusionReason.DURATION_UNKNOWN


def test_duration_falls_back_to_the_reports_own_timestamps():
    start = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    decision = decide_billing(
        _facts(started_at=start, ended_at=start + timedelta(seconds=95)), received_at=_RECEIVED
    )
    assert decision.duration_source is DurationSource.TIMESTAMPS
    assert decision.billable_seconds == 95
    assert decision.billable_minutes == 2


def test_timestamps_that_run_backwards_count_as_missing():
    start = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    decision = decide_billing(
        _facts(started_at=start, ended_at=start - timedelta(seconds=5)), received_at=_RECEIVED
    )
    assert decision.exclusion_reason is ExclusionReason.DURATION_UNKNOWN


def test_a_reported_duration_wins_over_timestamps():
    start = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    decision = decide_billing(
        _facts(
            started_at=start,
            ended_at=start + timedelta(seconds=300),
            reported_duration_seconds=Decimal("30"),
        ),
        received_at=_RECEIVED,
    )
    assert decision.duration_source is DurationSource.REPORTED
    assert decision.billable_minutes == 1


# --- fault ended reasons -----------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "pipeline-error-openai-llm-failed",
        "call.in-progress.error-vapifault-custom-llm-401-unauthorized",
        "call.start.error-get-assistant",
        "custom-llm-401-unauthorized",
        "assistant-request-failed",
        "phone-call-provider-closed-websocket",
        "twilio-failed-to-connect-call",
        "  Assistant-Error ",
    ],
)
def test_fault_ended_reasons_bill_nothing(reason: str):
    assert is_fault_ended_reason(reason) is True
    decision = decide_billing(
        _facts(reported_duration_seconds=Decimal("120"), ended_reason=reason), received_at=_RECEIVED
    )
    assert decision.is_billable is False
    assert decision.billable_minutes == 0
    # The duration is still recorded: excluded, not erased.
    assert decision.billable_seconds == 120
    assert decision.exclusion_reason is ExclusionReason.FAULT_ENDED_REASON


@pytest.mark.parametrize(
    "reason",
    [
        "customer-ended-call",
        "assistant-ended-call",
        "silence-timed-out",
        "exceeded-max-duration",
        "assistant-forwarded-call",
        "voicemail",
        "some-reason-vapi-adds-later",
        None,
    ],
)
def test_ordinary_and_unknown_ended_reasons_are_billable(reason: str | None):
    assert is_fault_ended_reason(reason) is False
    decision = decide_billing(
        _facts(reported_duration_seconds=Decimal("45"), ended_reason=reason), received_at=_RECEIVED
    )
    assert decision.is_billable is True


def test_the_minimum_is_applied_before_the_fault_rule():
    decision = decide_billing(
        _facts(reported_duration_seconds=Decimal("3"), ended_reason="pipeline-error-x"),
        received_at=_RECEIVED,
    )
    assert decision.exclusion_reason is ExclusionReason.UNDER_MINIMUM


# --- billing period ----------------------------------------------------------


def test_the_period_is_the_utc_month_of_the_call_start():
    # 23:30 on 31 Oct in New York is already 1 Nov in UTC.
    start = datetime(2026, 10, 31, 23, 30, tzinfo=timezone(timedelta(hours=-4)))
    decision = decide_billing(
        _facts(started_at=start, reported_duration_seconds=Decimal("60")), received_at=_RECEIVED
    )
    assert decision.billing_period == date(2026, 11, 1)
    assert decision.period_basis is PeriodBasis.STARTED_AT


def test_a_call_spanning_midnight_belongs_to_the_month_it_started():
    start = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
    decision = decide_billing(
        _facts(started_at=start, ended_at=start + timedelta(minutes=5)), received_at=_RECEIVED
    )
    assert decision.billing_period == date(2026, 10, 1)


def test_the_period_falls_back_to_the_end_then_to_receipt():
    end = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    by_end = decide_billing(_facts(ended_at=end), received_at=_RECEIVED)
    assert (by_end.billing_period, by_end.period_basis) == (date(2026, 9, 1), PeriodBasis.ENDED_AT)

    by_receipt = decide_billing(_facts(), received_at=_RECEIVED)
    assert (by_receipt.billing_period, by_receipt.period_basis) == (
        date(2026, 11, 1),
        PeriodBasis.RECEIVED_AT,
    )


# --- merging a repeated report -----------------------------------------------


def test_a_repeated_report_fills_gaps_and_never_erases():
    first = _facts(
        vapi_assistant_id="asst_1",
        reported_duration_seconds=Decimal("30"),
        vapi_reported_cost=Decimal("0.12"),
    )
    second = _facts(ended_reason="customer-ended-call", reported_duration_seconds=None)

    merged = merge_facts(first, second)

    assert merged.vapi_assistant_id == "asst_1"
    assert merged.reported_duration_seconds == Decimal("30")
    assert merged.vapi_reported_cost == Decimal("0.12")
    assert merged.ended_reason == "customer-ended-call"


def test_a_later_reported_value_replaces_the_earlier_one():
    merged = merge_facts(
        _facts(reported_duration_seconds=Decimal("30")),
        _facts(reported_duration_seconds=Decimal("95")),
    )
    assert merged.reported_duration_seconds == Decimal("95")


# --- reading Vapi's report ---------------------------------------------------


def test_the_parser_reads_every_known_field():
    facts = parse_end_of_call_usage(
        {
            "type": "end-of-call-report",
            "endedReason": "customer-ended-call",
            "durationSeconds": 74.5,
            "startedAt": "2026-11-03T09:00:00.000Z",
            "endedAt": "2026-11-03T09:01:14.500Z",
            "cost": 0.1234,
            "costBreakdown": {"vapi": 0.06, "stt": 0.01, "transport": 0, "total": 0.1234,
                              "analysisCostBreakdown": {"summary": 0.001}, "unknown": 5},
            "call": {"id": "call_p", "assistantId": "asst_p", "phoneNumberId": "pn_p"},
        },
        "call_p",
    )
    assert facts.vapi_call_id == "call_p"
    assert facts.vapi_assistant_id == "asst_p"
    assert facts.vapi_phone_number_id == "pn_p"
    assert facts.ended_reason == "customer-ended-call"
    assert facts.reported_duration_seconds == Decimal("74.5")
    assert facts.started_at == datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    assert facts.vapi_reported_cost == Decimal("0.1234")
    # Whitelisted, numeric components only; nested and unknown keys dropped.
    assert facts.vapi_reported_cost_breakdown == {
        "stt": Decimal("0.01"),
        "total": Decimal("0.1234"),
        "transport": Decimal("0"),
        "vapi": Decimal("0.06"),
    }


def test_the_parser_reads_cost_from_the_call_object_when_absent_at_top_level():
    facts = parse_end_of_call_usage(
        {"call": {"id": "c", "cost": "0.5", "costBreakdown": {"total": "0.5"}}}, "c"
    )
    assert facts.vapi_reported_cost == Decimal("0.5")
    assert facts.vapi_reported_cost_breakdown == {"total": Decimal("0.5")}


def test_the_parser_leaves_absent_fields_absent():
    facts = parse_end_of_call_usage({"call": {"id": "c"}}, "c")
    assert facts == CallUsageFacts(vapi_call_id="c")


@pytest.mark.parametrize(
    "bad",
    [-1, "abc", True, float("nan"), float("inf"), 10_000_000, [], {"x": 1}],
)
def test_implausible_or_malformed_values_become_unknown(bad):
    facts = parse_end_of_call_usage(
        {"durationSeconds": bad, "cost": bad, "costBreakdown": {"total": bad}, "call": {"id": "c"}},
        "c",
    )
    assert facts.reported_duration_seconds is None
    assert facts.vapi_reported_cost is None
    if bad != 10_000_000:
        # Breakdown components include token and character COUNTS, so only
        # their form is checked, not a currency-sized bound.
        assert facts.vapi_reported_cost_breakdown is None


def test_duration_in_milliseconds_is_used_only_when_seconds_are_absent():
    assert parse_end_of_call_usage(
        {"durationMs": 74500, "call": {"id": "c"}}, "c"
    ).reported_duration_seconds == Decimal("74.5")
    assert parse_end_of_call_usage(
        {"durationSeconds": 10, "durationMs": 74500, "call": {"id": "c"}}, "c"
    ).reported_duration_seconds == Decimal("10")


def test_a_naive_timestamp_is_not_assumed_to_be_utc():
    facts = parse_end_of_call_usage({"startedAt": "2026-11-03T09:00:00", "call": {"id": "c"}}, "c")
    assert facts.started_at is None


def test_an_overlong_identifier_is_dropped_not_truncated():
    facts = parse_end_of_call_usage({"call": {"id": "c", "assistantId": "a" * 129}}, "c")
    assert facts.vapi_assistant_id is None
