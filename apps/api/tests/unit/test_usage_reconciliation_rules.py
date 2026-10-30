"""The M2 reconciliation rules (`app/domain/usage_reconciliation.py`), the
reading of a Vapi `Call` object (`parse_vapi_call`), and the m2 fault
vocabulary. All pure functions."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.domain.usage import (
    RULES_VERSION,
    CallUsageFacts,
    CallUsageRecord,
    DurationSource,
    ExclusionReason,
    PeriodBasis,
    UsageAttribution,
    UsageSource,
    decide_billing,
    is_fault_ended_reason,
)
from app.domain.usage_reconciliation import (
    Discrepancy,
    ReconciliationOutcome,
    VapiCallSummary,
    classify,
    fill_missing,
    find_discrepancies,
)
from app.infrastructure.telephony.vapi_usage import parse_vapi_call

_START = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
_END = _START + timedelta(seconds=125)
_RECEIVED = _END + timedelta(seconds=5)


def _record(*, source: UsageSource = UsageSource.WEBHOOK, report_count: int = 1,
            **facts) -> CallUsageRecord:
    defaults = {"started_at": _START, "ended_at": _END, "ended_reason": "customer-ended-call",
                "vapi_reported_cost": Decimal("0.2125")}
    call_facts = CallUsageFacts(vapi_call_id="call_r", **{**defaults, **facts})
    return CallUsageRecord(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), conversation_id=None,
        attribution=UsageAttribution.VOICE_LINE, facts=call_facts,
        decision=decide_billing(call_facts, received_at=_RECEIVED), report_count=report_count,
        first_reported_at=_RECEIVED, last_reported_at=_RECEIVED, source=source,
    )


def _vapi(**overrides) -> VapiCallSummary:
    defaults = {"status": "ended", "vapi_assistant_id": "asst_r", "started_at": _START,
                "ended_at": _END, "ended_reason": "customer-ended-call",
                "cost": Decimal("0.2125")}
    return VapiCallSummary(vapi_call_id="call_r", **{**defaults, **overrides})


# --- m2 fault vocabulary -------------------------------------------------------


def test_the_rules_version_moved_to_m2():
    assert RULES_VERSION.startswith("m2-")


@pytest.mark.parametrize(
    "reason",
    [
        "call-start-error-neither-assistant-nor-server-set",
        "assistant-request-returned-unspeakable-error",
        "assistant-request-returned-invalid-assistant",
        "assistant-request-returned-no-assistant",
        "call.start.assistant-version-error-validation",
        "call.start.squad-version-error-validation",
        "worker-shutdown",
    ],
)
def test_m2_adds_the_failures_m1_missed(reason: str):
    assert is_fault_ended_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    ["assistant-request-returned-forwarding-phone-number", "vonage-disconnected",
     "call.in-progress.twilio-completed-call", "call.ringing.hook-executed-say"],
)
def test_m2_leaves_deliberate_or_ordinary_ends_billable(reason: str):
    assert is_fault_ended_reason(reason) is False


# --- fill_missing --------------------------------------------------------------


def test_only_missing_fields_are_filled():
    record = _record(vapi_reported_cost=None, ended_reason=None)
    filled_record, filled = fill_missing(
        record, _vapi(cost=Decimal("0.30"), ended_reason="silence-timed-out",
                      started_at=_START - timedelta(seconds=40))
    )
    assert set(filled) == {"vapi_reported_cost", "ended_reason", "vapi_assistant_id"}
    assert filled_record.facts.vapi_reported_cost == Decimal("0.30")
    assert filled_record.facts.ended_reason == "silence-timed-out"
    # A value we have is never overwritten, even when Vapi disagrees.
    assert filled_record.facts.started_at == _START


def test_nothing_to_fill_returns_the_record_unchanged():
    record = _record(vapi_assistant_id="asst_r")
    same, filled = fill_missing(record, _vapi())
    assert filled == []
    assert same is record


def test_a_duration_unknown_record_is_redecided_but_keeps_its_period():
    """M1 flags these for reconciliation. The decision is re-made from the
    filled timestamps, but the call stays in the month it was counted in."""
    record = _record(started_at=None, ended_at=None)
    assert record.decision.exclusion_reason is ExclusionReason.DURATION_UNKNOWN
    assert record.decision.period_basis is PeriodBasis.RECEIVED_AT
    # Vapi's timestamps fall in the previous month.
    early = datetime(2026, 9, 30, 23, 58, tzinfo=timezone.utc)
    filled_record, filled = fill_missing(
        record, _vapi(started_at=early, ended_at=early + timedelta(seconds=125))
    )
    assert {"started_at", "ended_at"} <= set(filled)
    decision = filled_record.decision
    assert decision.is_billable is True
    assert decision.billable_minutes == 3
    assert decision.duration_source is DurationSource.TIMESTAMPS
    assert decision.billing_period == record.decision.billing_period == date(2026, 10, 1)
    assert decision.period_basis is PeriodBasis.RECEIVED_AT
    assert decision.rules_version == RULES_VERSION


def test_a_decided_record_is_not_redecided_by_a_fill():
    """An ended reason learned later does not quietly un-bill a call; it is
    reported as a fault-classification discrepancy instead."""
    record = _record(ended_reason=None)
    filled_record, filled = fill_missing(record, _vapi(ended_reason="worker-shutdown"))
    assert "ended_reason" in filled
    assert filled_record.decision == record.decision
    assert Discrepancy.FAULT_CLASSIFICATION in find_discrepancies(
        filled_record, _vapi(ended_reason="worker-shutdown"))


def test_attribution_and_counters_are_never_touched():
    record = _record(vapi_reported_cost=None)
    filled_record, _ = fill_missing(record, _vapi())
    assert filled_record.organization_id == record.organization_id
    assert filled_record.attribution is record.attribution
    assert filled_record.report_count == record.report_count
    assert filled_record.first_reported_at == record.first_reported_at


# --- discrepancies and outcome -----------------------------------------------------


def test_agreement_is_matched():
    assert classify(_record(), _vapi(), filled=[]) == (ReconciliationOutcome.MATCHED, [])


def test_a_fill_without_disagreement_is_filled():
    assert classify(_record(), _vapi(), filled=["vapi_reported_cost"])[0] is (
        ReconciliationOutcome.FILLED)


def test_duration_within_a_second_matches_and_beyond_does_not():
    assert find_discrepancies(_record(), _vapi(ended_at=_END + timedelta(milliseconds=900))) == []
    assert find_discrepancies(_record(), _vapi(ended_at=_END + timedelta(seconds=2))) == [
        Discrepancy.DURATION]


def test_cost_and_ended_reason_disagreements_are_reported_not_resolved():
    outcome, found = classify(
        _record(), _vapi(cost=Decimal("0.25"), ended_reason="assistant-ended-call"), filled=[])
    assert outcome is ReconciliationOutcome.DISCREPANT
    assert found == [Discrepancy.ENDED_REASON, Discrepancy.COST]


def test_a_cost_difference_below_the_tolerance_matches():
    assert find_discrepancies(_record(), _vapi(cost=Decimal("0.21255"))) == []


def test_a_value_only_one_side_has_is_not_a_discrepancy():
    assert find_discrepancies(_record(vapi_reported_cost=None), _vapi()) == []
    assert find_discrepancies(_record(), _vapi(cost=None, started_at=None)) == []


def test_vapi_saying_its_cost_is_incomplete_is_surfaced():
    assert find_discrepancies(_record(), _vapi(usage_incomplete=True)) == [
        Discrepancy.COST_INCOMPLETE]


def test_a_reconciliation_created_record_stays_missing_locally_until_a_report_arrives():
    created = _record(source=UsageSource.RECONCILIATION, report_count=0)
    assert classify(created, _vapi(), filled=[])[0] is ReconciliationOutcome.MISSING_LOCALLY
    reported = _record(source=UsageSource.RECONCILIATION, report_count=1)
    assert classify(reported, _vapi(), filled=[])[0] is ReconciliationOutcome.MATCHED


# --- parse_vapi_call ---------------------------------------------------------------


def _call_json(**overrides) -> dict:
    """Shaped on Vapi's OpenAPI `Call` schema. Fictional ids and a 555
    number; the parser must drop the customer, transcript and recording."""
    call = {
        "id": "call_p", "orgId": "org_p", "type": "inboundPhoneCall", "status": "ended",
        "assistantId": "asst_p", "phoneNumberId": "pn_p",
        "createdAt": "2026-10-20T14:59:58.000Z", "updatedAt": "2026-10-20T15:03:00.000Z",
        "startedAt": "2026-10-20T15:00:00.000Z", "endedAt": "2026-10-20T15:02:05.500Z",
        "endedReason": "customer-ended-call", "cost": 0.2125,
        "costBreakdown": {"transport": 0.01, "vapi": 0.1, "llm": 0.05, "chat": 0,
                          "llmCachedPromptTokens": 900, "analysisCostBreakdown": {"summary": 1}},
        "costs": [
            {"type": "vapi", "subType": "normal", "minutes": 1.5, "cost": 0.075},
            {"type": "vapi", "subType": "overage", "minutes": 0.6, "cost": 0.03},
            {"type": "transport", "provider": "twilio", "minutes": 2.09, "cost": 0.01},
            {"type": "model", "promptTokens": 1000, "completionTokens": 50, "cost": 0.05,
             "usageComplete": True},
            {"type": "voice", "characters": 400, "cost": 0.02},
        ],
        "customer": {"number": "+16305550188"},
        "artifact": {"transcript": "secret words", "recordingUrl": "https://example.test/r"},
    }
    call.update(overrides)
    return call


def test_the_call_parser_reads_every_compared_field():
    summary = parse_vapi_call(_call_json())
    assert summary is not None
    assert summary.vapi_call_id == "call_p"
    assert summary.status == "ended" and summary.is_ended
    assert (summary.vapi_assistant_id, summary.vapi_phone_number_id) == ("asst_p", "pn_p")
    assert summary.duration_seconds == Decimal("125.5")
    assert summary.cost == Decimal("0.2125")
    assert summary.cost_breakdown == {"transport": Decimal("0.01"), "vapi": Decimal("0.1"),
                                      "llm": Decimal("0.05"), "chat": Decimal("0"),
                                      "llmCachedPromptTokens": Decimal("900")}
    assert summary.vapi_minutes_normal == Decimal("1.5")
    assert summary.vapi_minutes_overage == Decimal("0.6")
    assert summary.transport_minutes == Decimal("2.09")
    assert summary.usage_incomplete is False
    # Nothing about the caller survives parsing.
    assert "5550188" not in repr(summary) and "secret" not in repr(summary)


def test_model_usage_marked_incomplete_is_noted():
    summary = parse_vapi_call(_call_json(costs=[{"type": "model", "usageComplete": False}]))
    assert summary is not None and summary.usage_incomplete is True


def test_absent_or_malformed_line_items_stay_unknown():
    summary = parse_vapi_call(_call_json(costs=[
        "junk", {"type": "vapi", "subType": "weird", "minutes": 3},
        {"type": "transport", "minutes": -1}, {"type": "vapi", "subType": "normal",
                                                "minutes": "NaN"},
    ]))
    assert summary is not None
    assert summary.vapi_minutes_normal is None
    assert summary.vapi_minutes_overage is None
    assert summary.transport_minutes is None


@pytest.mark.parametrize("bad", [None, "x", [], {"status": "ended"}, {"id": ""}, {"id": 7}])
def test_an_object_without_a_usable_id_is_skipped(bad):
    assert parse_vapi_call(bad) is None


def test_an_in_progress_call_is_not_ended():
    summary = parse_vapi_call(_call_json(status="in-progress", endedAt=None))
    assert summary is not None
    assert summary.is_ended is False
    assert summary.duration_seconds is None
