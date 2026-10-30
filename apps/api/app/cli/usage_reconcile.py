"""Reconcile recorded call usage against Vapi's call API (usage metering M2).

Run inside the API container (operator access only; there is deliberately
no HTTP route for this, and nothing schedules it yet):

    docker compose exec api python -m app.cli.usage_reconcile --dry-run
    docker compose exec api python -m app.cli.usage_reconcile \
        [--since 2026-10-23] [--until 2026-10-30T12:00:00Z] [--dry-run]

The default window is the last USAGE_RECONCILE_LOOKBACK_DAYS days. Its end
is always pulled back by USAGE_RECONCILE_SETTLE_MINUTES. A date without a
time means midnight UTC. A time without a UTC offset is refused, because
guessing a zone could shift calls across a month boundary.

It makes only read-only GETs to Vapi, with VAPI_API_KEY (Vapi's private
key). It writes only to ESSR's own tables, in one transaction. `--dry-run`
does the whole run, prints what it would record, and then rolls back.

Exit codes:
- 0: the run completed. Calls listed under "needs a look" are findings,
  not failures.
- 1: the run failed and nothing was recorded. A FAILED run row is written
  unless this was a dry run.
- 2: refused (bad arguments, or no VAPI_API_KEY).
- 75: another reconciliation run is in progress.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.call_usage_service import CallUsageService
from app.application.services.usage_reconciliation_service import (
    ReconciliationReport,
    UsageReconciliationService,
)
from app.core.config import get_settings
from app.domain.usage_reconciliation import (
    ReconciliationAlreadyRunningError,
    ReconciliationRun,
    ReconciliationRunStatus,
    VapiCallSource,
)
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
from app.infrastructure.database.session import AsyncSessionLocal
from app.infrastructure.database.transactions import SqlAlchemySavepoints
from app.infrastructure.telephony.vapi_calls_client import VapiCallsClient

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_ALREADY_RUNNING = 0, 1, 2, 75
MAX_WINDOW = timedelta(days=31)


def _moment(value: str) -> datetime:
    text = value.strip()
    try:
        if "T" not in text and " " not in text:
            return datetime.combine(date.fromisoformat(text), time(), tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO date or date-time: {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"give a UTC offset (e.g. Z) for {value!r}")
    return parsed.astimezone(timezone.utc)


def _service(
    session: AsyncSession, vapi: VapiCallSource, clock: Callable[[], datetime] | None
) -> UsageReconciliationService:
    settings = get_settings()
    usage = SqlAlchemyCallUsageRepository(session)
    return UsageReconciliationService(
        vapi=vapi,
        reconciliation_repository=SqlAlchemyUsageReconciliationRepository(session),
        usage_repository=usage,
        call_usage_service=CallUsageService(
            usage_repository=usage,
            voice_call_repository=SqlAlchemyVoiceCallRepository(session),
            voice_line_repository=SqlAlchemyVoiceLineRepository(session),
            clock=clock,
        ),
        savepoints=SqlAlchemySavepoints(session),
        settle=timedelta(minutes=settings.USAGE_RECONCILE_SETTLE_MINUTES),
        clock=clock,
    )


def _print_report(report: ReconciliationReport, *, dry_run: bool) -> None:
    print(f"{'DRY RUN (rolled back, nothing recorded)' if dry_run else 'Recorded'}: "
          f"run {report.run_id}")
    print(f"Window: {report.window_start.isoformat()} -> {report.window_end.isoformat()}")
    if not report.counts:
        print("  no calls in this window")
    for name, count in sorted(report.counts.items()):
        print(f"  {name}: {count}")
    if report.attention:
        print("\nNeeds a look:")
        for vapi_call_id, outcome, discrepancies in report.attention:
            detail = f" ({', '.join(item.value for item in discrepancies)})" if discrepancies else ""
            print(f"  {vapi_call_id}: {outcome.value}{detail}")


async def _record_failure(
    session_factory: Callable[[], AsyncSession], start: datetime, end: datetime, error: str
) -> None:
    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            await SqlAlchemyUsageReconciliationRepository(session).save_run(ReconciliationRun(
                id=uuid.uuid4(), window_start=start, window_end=end, started_at=now,
                status=ReconciliationRunStatus.FAILED, finished_at=now, error=error[:64],
            ))
            await session.commit()
    except Exception as exc:
        print(f"(could not record the failed run either: {type(exc).__name__})", file=sys.stderr)


async def run(
    args: argparse.Namespace,
    *,
    vapi: VapiCallSource | None = None,
    session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
    clock: Callable[[], datetime] | None = None,
) -> int:
    settings = get_settings()
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    until = args.until or now
    since = args.since or until - timedelta(days=settings.USAGE_RECONCILE_LOOKBACK_DAYS)
    if since >= until or until - since > MAX_WINDOW:
        print(f"REFUSED: the window must be non-empty and at most {MAX_WINDOW.days} days.",
              file=sys.stderr)
        return EXIT_REFUSED
    if vapi is None:
        if not settings.VAPI_API_KEY or not settings.VAPI_API_KEY.strip():
            print("REFUSED: VAPI_API_KEY (Vapi's private key) is not set.", file=sys.stderr)
            return EXIT_REFUSED
        try:
            vapi = VapiCallsClient(api_key=settings.VAPI_API_KEY,
                                   base_url=settings.VAPI_API_BASE_URL)
        except ValueError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_REFUSED

    async with session_factory() as session:
        service = _service(session, vapi, clock)
        try:
            report = await service.run(since, until)
        except ReconciliationAlreadyRunningError:
            await session.rollback()
            print("Another usage reconciliation run is in progress; nothing done.",
                  file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        except ValueError as exc:
            await session.rollback()
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except Exception as exc:
            await session.rollback()
            error = type(exc).__name__
            print(f"FAILED ({error}): {exc if error.startswith('Vapi') else 'see logs'}; "
                  "nothing was recorded.", file=sys.stderr)
            if not args.dry_run:
                start, end = service.effective_window(since, until)
                await _record_failure(session_factory, start, end, error)
            return EXIT_FAILED
        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()
    _print_report(report, dry_run=args.dry_run)
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.usage_reconcile")
    parser.add_argument("--since", type=_moment, help="window start (inclusive), ISO, UTC")
    parser.add_argument("--until", type=_moment, help="window end (exclusive), ISO, UTC")
    parser.add_argument("--dry-run", action="store_true",
                        help="do the whole run, print the result, record nothing")
    return parser


def main() -> int:
    return asyncio.run(run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
