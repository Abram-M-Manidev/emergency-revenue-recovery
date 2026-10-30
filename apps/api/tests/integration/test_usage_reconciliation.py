"""Usage reconciliation (M2) end to end: the real CLI entry point, real
services and repositories, real Postgres. Only Vapi's call API is replaced,
by an in-memory `VapiCallSource`. No request leaves the process.

Pins down:
- a call Vapi has and we never got a report for is recorded, attributed
  through `voice_lines`, and marked as created by reconciliation;
- a report that arrives later merges into that record;
- a gap is filled, but a value we hold is never overwritten. A
  disagreement is recorded as a discrepancy instead;
- a local call Vapi 404s on is recorded as missing at Vapi;
- a second run over the same window creates nothing new and changes no
  usage record;
- a dry run writes nothing at all;
- a concurrent run is refused, and a Vapi failure aborts the run with no
  partial writes, leaving only a FAILED run row;
- a real database error on one call costs only that call.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.application.services.call_usage_service import CallUsageService
from app.cli import usage_reconcile
from app.domain.entities.voice_line import VoiceProvider
from app.domain.usage import CallUsageFacts, UsageAttribution, UsageSource
from app.domain.usage_reconciliation import (
    ReconciliationRunStatus,
    VapiAuthError,
    VapiCallSource,
    VapiCallSummary,
)
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.organization import OrganizationModel
from app.infrastructure.database.models.usage import (
    CallUsageRecordModel,
    UsageReconciliationRunModel,
    VapiCallReconciliationModel,
)
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import usage_repository_impl
from app.infrastructure.database.repositories.usage_repository_impl import (
    SqlAlchemyCallUsageRepository,
    SqlAlchemyUsageReconciliationRepository,
)
from app.infrastructure.database.repositories.voice_call_repository_impl import (
    SqlAlchemyVoiceCallRepository,
)
from app.infrastructure.database.repositories.voice_line_repository_impl import (
    SqlAlchemyVoiceLineRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.infrastructure.database.transactions import SqlAlchemySavepoints

_NOW = datetime.now(timezone.utc).replace(microsecond=0)


class _FakeVapi(VapiCallSource):
    def __init__(self, calls: list[VapiCallSummary] | None = None,
                 fail_with: Exception | None = None) -> None:
        self.calls = {call.vapi_call_id: call for call in calls or []}
        self.fail_with = fail_with
        self.gets: list[str] = []

    async def list_calls(self, start: datetime, end: datetime) -> list[VapiCallSummary]:
        if self.fail_with is not None:
            raise self.fail_with
        return [call for call in self.calls.values()
                if call.created_at is not None and start <= call.created_at < end]

    async def get_call(self, vapi_call_id: str) -> VapiCallSummary | None:
        self.gets.append(vapi_call_id)
        return self.calls.get(vapi_call_id)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _window(days_ago: int) -> tuple[datetime, datetime]:
    """Each test reconciles its own hour, so tests never see each other's
    calls."""
    start = _NOW - timedelta(days=days_ago)
    return start, start + timedelta(hours=1)


def _args(window: tuple[datetime, datetime], *, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(since=window[0], until=window[1], dry_run=dry_run)


async def _tenant() -> tuple[uuid.UUID, str]:
    organization_id = uuid.uuid4()
    assistant = f"asst_ur_{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as session:
        session.add(OrganizationModel(id=organization_id, name=f"UR {organization_id.hex[:6]}",
                                      slug=f"ur-{organization_id.hex[:10]}"))
        await session.flush()
        session.add(VoiceLineModel(organization_id=organization_id, provider=VoiceProvider.VAPI,
                                   vapi_assistant_id=assistant, is_active=True))
        await session.commit()
    return organization_id, assistant


def _vapi_call(window: tuple[datetime, datetime], assistant: str | None, **overrides
               ) -> VapiCallSummary:
    started = window[0] + timedelta(minutes=10)
    values = {"status": "ended", "vapi_assistant_id": assistant,
              "created_at": started - timedelta(seconds=2), "started_at": started,
              "ended_at": started + timedelta(seconds=125), "ended_reason": "customer-ended-call",
              "cost": Decimal("0.2125"), "vapi_minutes_normal": Decimal("2.0833")}
    values.update(overrides)
    return VapiCallSummary(vapi_call_id=f"call_ur_{uuid.uuid4().hex[:8]}", **values)


async def _report(facts: CallUsageFacts) -> None:
    """An end-of-call report, through the same service the webhook uses."""
    async with AsyncSessionLocal() as session:
        await CallUsageService(
            usage_repository=SqlAlchemyCallUsageRepository(session),
            voice_call_repository=SqlAlchemyVoiceCallRepository(session),
            voice_line_repository=SqlAlchemyVoiceLineRepository(session),
            savepoints=SqlAlchemySavepoints(session),
        ).record_end_of_call(facts)
        await session.commit()


async def _usage(call_id: str) -> CallUsageRecordModel | None:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(CallUsageRecordModel).where(
            CallUsageRecordModel.vapi_call_id == call_id))).scalar_one_or_none()


async def _reconciliation(call_id: str) -> VapiCallReconciliationModel | None:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(VapiCallReconciliationModel).where(
            VapiCallReconciliationModel.vapi_call_id == call_id))).scalar_one_or_none()


async def _count(model) -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio(loop_scope="session")
async def test_a_call_with_no_report_is_recorded_from_vapi(database_ready):
    organization_id, assistant = await _tenant()
    window = _window(2)
    call = _vapi_call(window, assistant)

    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([call])) == 0

    record = await _usage(call.vapi_call_id)
    assert record is not None
    assert record.source is UsageSource.RECONCILIATION
    assert record.report_count == 0
    assert record.organization_id == organization_id
    assert record.attribution is UsageAttribution.VOICE_LINE
    assert record.billable_minutes == 3 and record.is_billable is True
    assert record.vapi_reported_cost == Decimal("0.2125")
    row = await _reconciliation(call.vapi_call_id)
    assert row is not None
    assert row.outcome.value == "missing_locally"
    assert row.call_usage_record_id == record.id
    assert row.vapi_minutes_normal == Decimal("2.0833")

    # The report turns up after all: it merges into the same row.
    await _report(call.to_facts())
    merged = await _usage(call.vapi_call_id)
    assert merged is not None and merged.id == record.id
    assert merged.report_count == 1
    assert merged.source is UsageSource.RECONCILIATION
    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([call])) == 0
    row = await _reconciliation(call.vapi_call_id)
    assert row is not None and row.outcome.value == "matched" and row.check_count == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_gaps_are_filled_and_disagreements_recorded_never_overwritten(database_ready):
    _, assistant = await _tenant()
    window = _window(3)
    gap = _vapi_call(window, assistant)
    disagree = _vapi_call(window, assistant, cost=Decimal("0.50"))
    # Our reports: one without a cost, one with a different cost.
    await _report(CallUsageFacts(vapi_call_id=gap.vapi_call_id, vapi_assistant_id=assistant,
                                 started_at=gap.started_at, ended_at=gap.ended_at,
                                 ended_reason="customer-ended-call"))
    await _report(replace(disagree.to_facts(), vapi_reported_cost=Decimal("0.2125")))

    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([gap, disagree])) == 0

    filled = await _usage(gap.vapi_call_id)
    assert filled is not None and filled.vapi_reported_cost == Decimal("0.2125")
    assert filled.source is UsageSource.WEBHOOK
    row = await _reconciliation(gap.vapi_call_id)
    assert row is not None and row.outcome.value == "filled"
    assert row.filled_fields == ["vapi_reported_cost"]

    kept = await _usage(disagree.vapi_call_id)
    assert kept is not None and kept.vapi_reported_cost == Decimal("0.2125")
    row = await _reconciliation(disagree.vapi_call_id)
    assert row is not None and row.outcome.value == "discrepant"
    assert row.discrepancies == ["cost"]
    assert row.vapi_cost == Decimal("0.5")

    # A second run fills nothing more, but remembers what the first filled.
    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([gap, disagree])) == 0
    row = await _reconciliation(gap.vapi_call_id)
    assert row is not None and row.outcome.value == "matched"
    assert row.filled_fields == ["vapi_reported_cost"] and row.check_count == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_local_call_vapi_does_not_list_is_confirmed_by_id(database_ready):
    _, assistant = await _tenant()
    window = _window(4)
    gone = _vapi_call(window, assistant)
    late = _vapi_call(window, assistant)
    await _report(gone.to_facts())
    await _report(late.to_facts())
    # `late` is not in the listing (created just before the window) but
    # GET /call/{id} finds it; `gone` is a 404.
    vapi = _FakeVapi([replace(late, created_at=window[0] - timedelta(seconds=1))])

    assert await usage_reconcile.run(_args(window), vapi=vapi) == 0

    assert sorted(vapi.gets) == sorted([gone.vapi_call_id, late.vapi_call_id])
    gone_row = await _reconciliation(gone.vapi_call_id)
    assert gone_row is not None and gone_row.outcome.value == "missing_at_vapi"
    assert gone_row.call_usage_record_id is not None
    late_row = await _reconciliation(late.vapi_call_id)
    assert late_row is not None and late_row.outcome.value == "matched"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_call_vapi_has_not_ended_is_left_alone(database_ready):
    _, assistant = await _tenant()
    window = _window(5)
    live = _vapi_call(window, assistant, status="in-progress", ended_at=None)
    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([live])) == 0
    assert await _usage(live.vapi_call_id) is None
    row = await _reconciliation(live.vapi_call_id)
    assert row is not None and row.outcome.value == "not_ended"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rerun_changes_no_usage_record(database_ready):
    _, assistant = await _tenant()
    window = _window(6)
    calls = [_vapi_call(window, assistant), _vapi_call(window, None)]
    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi(calls)) == 0
    before = {c.vapi_call_id: await _usage(c.vapi_call_id) for c in calls}
    records = await _count(CallUsageRecordModel)

    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi(calls)) == 0

    assert await _count(CallUsageRecordModel) == records
    for call in calls:
        after = await _usage(call.vapi_call_id)
        old = before[call.vapi_call_id]
        assert after is not None and old is not None
        assert (after.billable_minutes, after.updated_at, after.organization_id) == (
            old.billable_minutes, old.updated_at, old.organization_id)
    # The unattributed one is kept without a tenant, never guessed.
    unattributed = before[calls[1].vapi_call_id]
    assert unattributed is not None and unattributed.organization_id is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_dry_run_writes_nothing(database_ready, capsys):
    _, assistant = await _tenant()
    window = _window(7)
    call = _vapi_call(window, assistant)
    counts = [await _count(m) for m in (CallUsageRecordModel, VapiCallReconciliationModel,
                                        UsageReconciliationRunModel)]

    assert await usage_reconcile.run(_args(window, dry_run=True), vapi=_FakeVapi([call])) == 0

    assert [await _count(m) for m in (CallUsageRecordModel, VapiCallReconciliationModel,
                                      UsageReconciliationRunModel)] == counts
    output = capsys.readouterr().out
    assert "DRY RUN" in output and call.vapi_call_id in output and "missing_locally" in output


@pytest.mark.asyncio(loop_scope="session")
async def test_a_concurrent_run_is_refused(database_ready):
    window = _window(8)
    async with AsyncSessionLocal() as holder:
        assert await SqlAlchemyUsageReconciliationRepository(holder).try_lock() is True
        assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([])) == 75
        await holder.rollback()
    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([])) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_vapi_failure_records_only_a_failed_run(database_ready):
    window = _window(9)
    reconciliations = await _count(VapiCallReconciliationModel)

    code = await usage_reconcile.run(_args(window), vapi=_FakeVapi(fail_with=VapiAuthError("401")))

    assert code == 1
    assert await _count(VapiCallReconciliationModel) == reconciliations
    async with AsyncSessionLocal() as session:
        run = (await session.execute(select(UsageReconciliationRunModel).where(
            UsageReconciliationRunModel.window_start == window[0]))).scalar_one()
    assert run.status is ReconciliationRunStatus.FAILED
    assert run.error == "VapiAuthError"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_database_error_on_one_call_costs_only_that_call(database_ready, monkeypatch):
    _, assistant = await _tenant()
    window = _window(10)
    broken, fine = _vapi_call(window, assistant), _vapi_call(window, assistant)
    original = usage_repository_impl.SqlAlchemyUsageReconciliationRepository.upsert

    async def upsert(self, result):
        if result.vapi_call_id == broken.vapi_call_id:
            await self._session.execute(text("SELECT 1/0"))  # a REAL aborted statement
        await original(self, result)

    monkeypatch.setattr(usage_repository_impl.SqlAlchemyUsageReconciliationRepository,
                        "upsert", upsert)

    assert await usage_reconcile.run(_args(window), vapi=_FakeVapi([broken, fine])) == 0

    assert await _usage(broken.vapi_call_id) is None  # its savepoint was undone
    assert await _reconciliation(broken.vapi_call_id) is None
    assert await _usage(fine.vapi_call_id) is not None
    assert await _reconciliation(fine.vapi_call_id) is not None
    async with AsyncSessionLocal() as session:
        run = (await session.execute(select(UsageReconciliationRunModel).where(
            UsageReconciliationRunModel.window_start == window[0]))).scalar_one()
    assert run.status is ReconciliationRunStatus.SUCCEEDED
    assert run.counts is not None and run.counts["errors"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_bad_windows_and_a_missing_key_are_refused(database_ready, monkeypatch):
    start = _NOW - timedelta(days=40)
    assert await usage_reconcile.run(argparse.Namespace(
        since=start, until=_NOW, dry_run=False), vapi=_FakeVapi([])) == 2
    assert await usage_reconcile.run(argparse.Namespace(
        since=_NOW, until=_NOW - timedelta(hours=1), dry_run=False), vapi=_FakeVapi([])) == 2
    # Entirely inside the settle delay: nothing could be reconciled yet.
    assert await usage_reconcile.run(argparse.Namespace(
        since=_NOW - timedelta(minutes=5), until=_NOW, dry_run=False), vapi=_FakeVapi([])) == 2
    monkeypatch.setattr(usage_reconcile.get_settings(), "VAPI_API_KEY", None)
    assert await usage_reconcile.run(_args(_window(11))) == 2
