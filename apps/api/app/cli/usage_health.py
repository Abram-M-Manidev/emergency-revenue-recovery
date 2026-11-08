"""Operator health check for usage data (metering M4).

    python -m app.cli.usage_health [--max-age-hours 26]

Exit 0: usage data is trustworthy. Exit 1: at least one problem, each
printed as a code and logged at ERROR as `usage_health_problem` — the event
the log-based alert in docs/RUNBOOK.md ("Usage reconciliation") watches.
Exit 2: the check itself could not run.

Read-only. Problem codes:
- reconciliation_never_run / reconciliation_last_run_failed /
  reconciliation_stale: run `python -m app.cli.usage_reconcile` and read
  its output; Vapi keeps call data for 14 days only on the current plan.
- calls_missing_usage: calls in the last 7 days with no usage record — the
  end-of-call webhook was missed. The next reconciliation backfills them.
- unattributed_usage: Vapi billed calls that map to no organization (an
  assistant or number not provisioned in `voice_lines`).
- reconciliation_discrepancies: see `vapi_call_reconciliations` rows whose
  outcome is not MATCHED/FILLED/NOT_ENDED.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

import structlog

from app.application.services.usage_report_service import UsageReportService
from app.infrastructure.database.repositories.usage_report_repository_impl import (
    SqlAlchemyUsageReportRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal

logger = structlog.get_logger("app.usage.health")


async def _check(max_age: timedelta) -> int:
    try:
        async with AsyncSessionLocal() as session:
            service = UsageReportService(
                usage_report_repository=SqlAlchemyUsageReportRepository(session)
            )
            health = await service.health(max_reconciliation_age=max_age)
    except Exception as exc:
        logger.error("usage_health_check_failed", error=type(exc).__name__)
        print(f"check failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    if health.healthy:
        logger.info("usage_health_ok")
        print("ok")
        return 0
    for problem in health.problems:
        logger.error("usage_health_problem", problem=problem)
        print(problem)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--max-age-hours", type=float, default=26.0)
    args = parser.parse_args(argv)
    return asyncio.run(_check(timedelta(hours=args.max_age_hours)))


if __name__ == "__main__":
    raise SystemExit(main())
