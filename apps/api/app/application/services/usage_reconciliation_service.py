"""One usage-reconciliation run: Vapi's view of every call in a window,
checked against `call_usage_records`. See `app/domain/usage_reconciliation.py`
for the rules, and `app/cli/usage_reconcile.py` for how a run is started.

Order of work, chosen so that Vapi is asked everything before anything is
written:

1. List every call Vapi created in the window. A failure here aborts the
   run before any write, because a partial listing would misreport every
   call it missed.
2. Take the reconciliation lock (non-blocking). A second concurrent run
   stops here.
3. Confirm each local record the listing did not include with `GET
   /call/{id}`. A call created just before the window, or listed late, is
   found that way. Only a 404 counts as missing at Vapi.
4. Reconcile each call in its own savepoint. One bad call is logged and
   counted, and the rest of the run continues.

Everything happens in the caller's single transaction. The caller commits
it, or rolls it back for a dry run, which is how a dry run writes nothing.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import structlog

from app.application.services.call_usage_service import CallUsageService
from app.domain.repositories.usage_repository import (
    CallUsageRepository,
    UsageReconciliationRepository,
)
from app.domain.transactions import NullSavepoints, Savepoints
from app.domain.usage_reconciliation import (
    CallReconciliation,
    Discrepancy,
    ReconciliationAlreadyRunningError,
    ReconciliationOutcome,
    ReconciliationRun,
    ReconciliationRunStatus,
    VapiCallSource,
    VapiCallSummary,
    classify,
    fill_missing,
)

logger = structlog.get_logger("app.usage.reconcile")


@dataclass
class ReconciliationReport:
    run_id: uuid.UUID
    window_start: datetime
    window_end: datetime
    counts: Counter[str] = field(default_factory=Counter)
    # (vapi_call_id, outcome, discrepancies) for every call that did not
    # simply match, so an operator sees exactly what needs a look.
    attention: list[tuple[str, ReconciliationOutcome, list[Discrepancy]]] = field(
        default_factory=list
    )

    @property
    def errors(self) -> int:
        return self.counts["errors"]


class UsageReconciliationService:
    def __init__(
        self,
        *,
        vapi: VapiCallSource,
        reconciliation_repository: UsageReconciliationRepository,
        usage_repository: CallUsageRepository,
        call_usage_service: CallUsageService,
        savepoints: Savepoints | None = None,
        settle: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vapi = vapi
        self._reconciliations = reconciliation_repository
        self._usage = usage_repository
        self._call_usage = call_usage_service
        self._savepoints = savepoints or NullSavepoints()
        self._settle = settle
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def effective_window(self, since: datetime, until: datetime) -> tuple[datetime, datetime]:
        """The window actually reconciled: `until` is pulled back so a call
        that ended in the last few minutes has time to settle its cost."""
        return since, min(until, self._clock() - self._settle)

    async def run(self, since: datetime, until: datetime) -> ReconciliationReport:
        start, end = self.effective_window(since, until)
        if end <= start:
            raise ValueError("The window is empty once the settle delay is applied.")
        run_id = uuid.uuid4()
        report = ReconciliationReport(run_id=run_id, window_start=start, window_end=end)
        logger.info("usage_reconcile_started", run_id=str(run_id),
                    window_start=start.isoformat(), window_end=end.isoformat())

        listed = {call.vapi_call_id: call for call in await self._vapi.list_calls(start, end)}

        if not await self._reconciliations.try_lock():
            raise ReconciliationAlreadyRunningError()
        started_at = self._clock()
        await self._reconciliations.save_run(ReconciliationRun(
            id=run_id, window_start=start, window_end=end, started_at=started_at,
            status=ReconciliationRunStatus.RUNNING,
        ))

        missing_at_vapi: list[str] = []
        for vapi_call_id in sorted(
            await self._reconciliations.local_call_ids_in_window(start, end) - listed.keys()
        ):
            found = await self._vapi.get_call(vapi_call_id)
            if found is None:
                missing_at_vapi.append(vapi_call_id)
            else:
                listed[vapi_call_id] = found

        for call in listed.values():
            await self._contained(report, call.vapi_call_id, self._reconcile(run_id, call))
        for vapi_call_id in missing_at_vapi:
            await self._contained(report, vapi_call_id, self._missing_at_vapi(run_id, vapi_call_id))

        await self._reconciliations.save_run(ReconciliationRun(
            id=run_id, window_start=start, window_end=end, started_at=started_at,
            status=ReconciliationRunStatus.SUCCEEDED, finished_at=self._clock(),
            counts=dict(report.counts),
        ))
        logger.info("usage_reconcile_finished", run_id=str(run_id), **dict(report.counts))
        return report

    async def _contained(
        self,
        report: ReconciliationReport,
        vapi_call_id: str,
        work: Awaitable[tuple[CallReconciliation, bool]],
    ) -> None:
        try:
            # Savepoint inside the `try`: catching a database error does not
            # un-abort a Postgres transaction.
            async with self._savepoints.isolate():
                result, created = await work
        except Exception as exc:
            report.counts["errors"] += 1
            # Type only: a driver error quotes the offending parameters.
            logger.error("usage_reconcile_call_failed", vapi_call_id=vapi_call_id,
                         error=type(exc).__name__)
            return
        report.counts[result.outcome.value] += 1
        if created:
            report.counts["created"] += 1
        if result.filled_fields:
            report.counts["filled"] += 1
        if result.outcome is not ReconciliationOutcome.MATCHED:
            report.attention.append((vapi_call_id, result.outcome, result.discrepancies))
            logger.warning("usage_reconcile_attention", vapi_call_id=vapi_call_id,
                           outcome=result.outcome.value,
                           discrepancies=[item.value for item in result.discrepancies],
                           filled_fields=result.filled_fields)

    async def _reconcile(
        self, run_id: uuid.UUID, vapi: VapiCallSummary
    ) -> tuple[CallReconciliation, bool]:
        record = await self._usage.get_for_update(vapi.vapi_call_id)
        created = False
        filled: list[str] = []
        discrepancies: list[Discrepancy] = []
        if not vapi.is_ended:
            outcome = ReconciliationOutcome.NOT_ENDED
        else:
            if record is None:
                record = await self._call_usage.create_from_reconciliation(vapi.to_facts())
                created = record is not None
                if record is None:
                    # An end-of-call report won the race: reconcile its row.
                    record = await self._usage.get_for_update(vapi.vapi_call_id)
                    if record is None:  # pragma: no cover - insert said it exists
                        raise RuntimeError("usage record vanished during reconciliation")
            if not created:
                record, filled = fill_missing(record, vapi)
                if filled:
                    record = await self._usage.update(record)
            outcome, discrepancies = classify(record, vapi, filled=filled)
        result = CallReconciliation(
            vapi_call_id=vapi.vapi_call_id,
            run_id=run_id,
            outcome=outcome,
            discrepancies=discrepancies,
            filled_fields=filled,
            checked_at=self._clock(),
            call_usage_record_id=record.id if record is not None else None,
            vapi=vapi,
        )
        await self._reconciliations.upsert(result)
        return result, created

    async def _missing_at_vapi(
        self, run_id: uuid.UUID, vapi_call_id: str
    ) -> tuple[CallReconciliation, bool]:
        record = await self._usage.get_for_update(vapi_call_id)
        result = CallReconciliation(
            vapi_call_id=vapi_call_id,
            run_id=run_id,
            outcome=ReconciliationOutcome.MISSING_AT_VAPI,
            discrepancies=[],
            filled_fields=[],
            checked_at=self._clock(),
            call_usage_record_id=record.id if record is not None else None,
        )
        await self._reconciliations.upsert(result)
        return result, False
