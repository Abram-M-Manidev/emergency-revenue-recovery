"""Metering M4: when usage data must not be trusted, and say so."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.usage_report import UsageHealthFacts, evaluate_usage_health

_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _facts(**overrides) -> UsageHealthFacts:
    values = dict(
        now=_NOW,
        last_run_started_at=_NOW - timedelta(hours=3),
        last_run_status="succeeded",
        last_success_finished_at=_NOW - timedelta(hours=3),
        calls_missing_usage=0,
        unattributed_records=0,
        discrepant_calls=0,
    )
    values.update(overrides)
    return UsageHealthFacts(**values)


def test_a_recent_clean_run_is_healthy():
    health = evaluate_usage_health(_facts())
    assert health.healthy and health.problems == []


def test_never_having_reconciled_is_a_problem():
    health = evaluate_usage_health(
        _facts(last_run_started_at=None, last_run_status=None, last_success_finished_at=None)
    )
    assert health.problems == ["reconciliation_never_run"]


def test_a_failed_last_run_is_reported_even_after_an_earlier_success():
    health = evaluate_usage_health(_facts(last_run_status="failed"))
    assert "reconciliation_last_run_failed" in health.problems
    assert "reconciliation_stale" not in health.problems


def test_a_success_older_than_the_limit_is_stale():
    stale = _facts(last_success_finished_at=_NOW - timedelta(hours=27))
    assert evaluate_usage_health(stale).problems == ["reconciliation_stale"]
    assert evaluate_usage_health(stale, max_reconciliation_age=timedelta(hours=48)).healthy


def test_runs_that_never_succeeded_are_stale_too():
    health = evaluate_usage_health(_facts(last_run_status="failed", last_success_finished_at=None))
    assert health.problems == ["reconciliation_last_run_failed", "reconciliation_stale"]


def test_data_problems_are_each_reported():
    health = evaluate_usage_health(
        _facts(calls_missing_usage=2, unattributed_records=1, discrepant_calls=3)
    )
    assert health.problems == [
        "calls_missing_usage",
        "unattributed_usage",
        "reconciliation_discrepancies",
    ]
