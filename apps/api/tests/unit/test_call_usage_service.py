"""`CallUsageService`: attribution, idempotent merging of repeated reports,
and containment — a metering failure is logged and swallowed, never
raised into the end-of-call report."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.application.services.call_usage_service import CallUsageService
from app.domain.entities.voice_call import VoiceCall
from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.repositories.usage_repository import CallUsageRepository
from app.domain.usage import (
    CallUsageFacts,
    CallUsageRecord,
    ExclusionReason,
    PeriodBasis,
    UsageAttribution,
)

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
_ORG = uuid.uuid4()
_OTHER_ORG = uuid.uuid4()


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


class _UsageRepo(CallUsageRepository):
    def __init__(self) -> None:
        self.rows: dict[str, CallUsageRecord] = {}
        self.fail = False
        # Simulates a concurrent delivery inserting between our read and
        # our insert.
        self.race_with: CallUsageRecord | None = None

    async def get_for_update(self, vapi_call_id: str) -> CallUsageRecord | None:
        if self.fail:
            raise RuntimeError("database is down")
        return self.rows.get(vapi_call_id)

    async def insert_if_absent(self, record: CallUsageRecord) -> bool:
        if self.race_with is not None:
            self.rows[record.vapi_call_id] = self.race_with
            self.race_with = None
            return False
        if record.vapi_call_id in self.rows:
            return False
        self.rows[record.vapi_call_id] = record
        return True

    async def update(self, record: CallUsageRecord) -> CallUsageRecord:
        self.rows[record.vapi_call_id] = record
        return record


class _VoiceCalls:
    def __init__(self, calls: dict[str, VoiceCall] | None = None) -> None:
        self.calls = calls or {}

    async def get_by_vapi_call_id(self, vapi_call_id: str) -> VoiceCall | None:
        return self.calls.get(vapi_call_id)


class _VoiceLines:
    def __init__(self, lines: list[VoiceLine] | None = None) -> None:
        self.lines = lines or []

    async def get_by_vapi_assistant_id(self, assistant_id: str) -> VoiceLine | None:
        return next((line for line in self.lines if line.vapi_assistant_id == assistant_id), None)

    async def get_by_vapi_phone_number_id(self, phone_number_id: str) -> VoiceLine | None:
        return next(
            (line for line in self.lines if line.vapi_phone_number_id == phone_number_id), None
        )


def _line(organization_id: uuid.UUID, assistant: str, phone_id: str | None = None,
          *, active: bool = True) -> VoiceLine:
    return VoiceLine(
        id=uuid.uuid4(), organization_id=organization_id, provider=VoiceProvider.VAPI,
        vapi_assistant_id=assistant, vapi_phone_number_id=phone_id, phone_number=None,
        is_active=active, created_at=_NOW, updated_at=_NOW,
    )


def _voice_call(vapi_call_id: str, organization_id: uuid.UUID) -> VoiceCall:
    return VoiceCall(
        id=uuid.uuid4(), organization_id=organization_id, conversation_id=uuid.uuid4(),
        vapi_call_id=vapi_call_id, caller_number=None, started_at=_NOW, ended_at=None,
        ended_reason=None, duration_seconds=None, recording_url=None, created_at=_NOW,
        updated_at=_NOW,
    )


def _service(repo: _UsageRepo, *, calls=None, lines=None, clock=None) -> CallUsageService:
    return CallUsageService(
        usage_repository=repo,
        voice_call_repository=_VoiceCalls(calls),  # type: ignore[arg-type]
        voice_line_repository=_VoiceLines(lines),  # type: ignore[arg-type]
        clock=clock or _Clock(),
    )


def _facts(call_id: str = "call_1", **overrides) -> CallUsageFacts:
    defaults = {"reported_duration_seconds": Decimal("125"), "ended_reason": "customer-ended-call"}
    return CallUsageFacts(vapi_call_id=call_id, **{**defaults, **overrides})


@pytest.mark.asyncio
async def test_a_call_with_a_conversation_is_attributed_through_its_voice_call():
    repo = _UsageRepo()
    voice_call = _voice_call("call_1", _ORG)
    record = await _service(repo, calls={"call_1": voice_call}).record_end_of_call(_facts())

    assert record is not None
    assert record.attribution is UsageAttribution.VOICE_CALL
    assert record.organization_id == _ORG
    assert record.conversation_id == voice_call.conversation_id
    assert record.decision.billable_minutes == 3
    assert record.decision.billing_period == date(2026, 11, 1)


@pytest.mark.asyncio
async def test_a_call_with_no_conversation_is_attributed_through_its_voice_line():
    """The caller hung up before saying anything: no `voice_calls` row, but
    Vapi still bills the call."""
    repo = _UsageRepo()
    record = await _service(repo, lines=[_line(_ORG, "asst_1")]).record_end_of_call(
        _facts(vapi_assistant_id="asst_1")
    )

    assert record is not None
    assert record.attribution is UsageAttribution.VOICE_LINE
    assert (record.organization_id, record.conversation_id) == (_ORG, None)


@pytest.mark.asyncio
async def test_an_inactive_line_still_attributes_its_cost():
    repo = _UsageRepo()
    record = await _service(repo, lines=[_line(_ORG, "asst_1", active=False)]).record_end_of_call(
        _facts(vapi_assistant_id="asst_1")
    )
    assert record is not None and record.organization_id == _ORG


@pytest.mark.asyncio
async def test_the_phone_number_is_used_only_when_there_is_no_assistant_id():
    repo = _UsageRepo()
    lines = [_line(_ORG, "asst_1", "pn_1")]
    by_number = await _service(repo, lines=lines).record_end_of_call(
        _facts("call_pn", vapi_phone_number_id="pn_1")
    )
    assert by_number is not None and by_number.organization_id == _ORG

    # An assistant we do not know is never resolved through the number:
    # that would bill a tenant for an assistant nobody provisioned to it.
    unknown_assistant = await _service(repo, lines=lines).record_end_of_call(
        _facts("call_mismatch", vapi_assistant_id="asst_unknown", vapi_phone_number_id="pn_1")
    )
    assert unknown_assistant is not None
    assert unknown_assistant.attribution is UsageAttribution.UNATTRIBUTED
    assert unknown_assistant.organization_id is None


@pytest.mark.asyncio
async def test_an_unmatched_call_is_kept_unattributed():
    repo = _UsageRepo()
    record = await _service(repo).record_end_of_call(_facts(vapi_assistant_id="asst_nobody"))

    assert record is not None
    assert record.attribution is UsageAttribution.UNATTRIBUTED
    assert record.organization_id is None
    assert record.decision.billable_minutes == 3  # still measured, for the operator
    assert "call_1" in repo.rows


@pytest.mark.asyncio
async def test_a_duplicate_report_merges_into_one_record():
    repo = _UsageRepo()
    clock = _Clock()
    service = _service(repo, lines=[_line(_ORG, "asst_1")], clock=clock)

    first = await service.record_end_of_call(_facts(vapi_assistant_id="asst_1"))
    clock.now = _NOW + timedelta(minutes=2)
    second = await service.record_end_of_call(_facts(vapi_assistant_id="asst_1"))

    assert len(repo.rows) == 1
    assert first is not None and second is not None
    assert second.id == first.id
    assert second.report_count == 2
    assert second.first_reported_at == _NOW
    assert second.last_reported_at == _NOW + timedelta(minutes=2)
    assert second.decision == first.decision


@pytest.mark.asyncio
async def test_a_later_report_completes_a_missing_duration():
    repo = _UsageRepo()
    service = _service(repo, lines=[_line(_ORG, "asst_1")])

    first = await service.record_end_of_call(
        _facts(vapi_assistant_id="asst_1", reported_duration_seconds=None)
    )
    assert first is not None
    assert first.decision.exclusion_reason is ExclusionReason.DURATION_UNKNOWN
    assert first.decision.billable_minutes == 0

    second = await service.record_end_of_call(_facts(reported_duration_seconds=Decimal("61")))
    assert second is not None
    assert second.decision.is_billable is True
    assert second.decision.billable_minutes == 2
    assert second.facts.vapi_assistant_id == "asst_1"  # not erased by the second report


@pytest.mark.asyncio
async def test_a_duplicate_cannot_move_a_call_between_months():
    """With no timestamps the period comes from receipt — the FIRST
    receipt, so a retry delivered after midnight on the 1st stays put."""
    repo = _UsageRepo()
    clock = _Clock()
    clock.now = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
    service = _service(repo, clock=clock)

    await service.record_end_of_call(_facts())
    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    again = await service.record_end_of_call(_facts())

    assert again is not None
    assert again.decision.period_basis is PeriodBasis.RECEIVED_AT
    assert again.decision.billing_period == date(2026, 10, 1)


@pytest.mark.asyncio
async def test_an_unattributed_record_is_attributed_once_a_line_exists():
    repo = _UsageRepo()
    await _service(repo).record_end_of_call(_facts(vapi_assistant_id="asst_late"))

    later = await _service(repo, lines=[_line(_ORG, "asst_late")]).record_end_of_call(_facts())

    assert later is not None
    assert later.attribution is UsageAttribution.VOICE_LINE
    assert later.organization_id == _ORG


@pytest.mark.asyncio
async def test_an_attributed_record_is_never_re_attributed():
    repo = _UsageRepo()
    await _service(repo, lines=[_line(_ORG, "asst_1")]).record_end_of_call(
        _facts(vapi_assistant_id="asst_1")
    )
    # The assistant was since moved to another tenant.
    later = await _service(repo, lines=[_line(_OTHER_ORG, "asst_1")]).record_end_of_call(
        _facts(vapi_assistant_id="asst_1")
    )
    assert later is not None and later.organization_id == _ORG


@pytest.mark.asyncio
async def test_losing_a_concurrent_insert_merges_into_the_winner():
    repo = _UsageRepo()
    winner = CallUsageRecord(
        id=uuid.uuid4(), organization_id=_ORG, conversation_id=None,
        attribution=UsageAttribution.VOICE_LINE,
        facts=_facts(reported_duration_seconds=None, ended_reason=None),
        decision=(await _service(_UsageRepo()).record_end_of_call(_facts())).decision,  # type: ignore[union-attr]
        report_count=1, first_reported_at=_NOW, last_reported_at=_NOW,
    )
    repo.race_with = winner

    record = await _service(repo).record_end_of_call(_facts())

    assert record is not None
    assert record.id == winner.id
    assert record.report_count == 2
    assert record.facts.reported_duration_seconds == Decimal("125")
    assert len(repo.rows) == 1


@pytest.mark.asyncio
async def test_a_repository_failure_is_swallowed():
    repo = _UsageRepo()
    repo.fail = True

    assert await _service(repo).record_end_of_call(_facts()) is None


@pytest.mark.asyncio
async def test_vapi_cost_is_stored_only_as_reported():
    repo = _UsageRepo()
    no_cost = await _service(repo).record_end_of_call(_facts("call_nc"))
    with_cost = await _service(repo).record_end_of_call(
        _facts("call_c", vapi_reported_cost=Decimal("0.4100"),
               vapi_reported_cost_breakdown={"vapi": Decimal("0.10")})
    )

    assert no_cost is not None and no_cost.facts.vapi_reported_cost is None
    assert no_cost.facts.vapi_reported_cost_breakdown is None
    assert with_cost is not None
    assert with_cost.facts == replace(
        _facts("call_c"),
        vapi_reported_cost=Decimal("0.4100"),
        vapi_reported_cost_breakdown={"vapi": Decimal("0.10")},
    )
