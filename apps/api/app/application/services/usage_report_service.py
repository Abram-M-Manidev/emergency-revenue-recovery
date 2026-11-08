from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from app.domain.repositories.usage_report_repository import UsageReportRepository
from app.domain.usage_report import (
    DEFAULT_MAX_RECONCILIATION_AGE,
    UsageHealth,
    UsageSummary,
    evaluate_usage_health,
)

#: How far back the health check looks for calls and records.
HEALTH_LOOKBACK = timedelta(days=7)
#: How long after a call starts its end-of-call report is allowed to take.
#: A call longer than this simply shows up on the next check.
USAGE_REPORT_GRACE = timedelta(hours=2)


def current_billing_period(now: datetime | None = None) -> date:
    """The UTC month, as the first day — the same period rule M1 records."""
    now = now or datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


class UsageReportService:
    def __init__(self, *, usage_report_repository: UsageReportRepository) -> None:
        self._reports = usage_report_repository

    async def summary(self, organization_id: uuid.UUID, billing_period: date) -> UsageSummary:
        return await self._reports.summary(organization_id, billing_period.replace(day=1))

    async def health(
        self,
        *,
        now: datetime | None = None,
        max_reconciliation_age: timedelta = DEFAULT_MAX_RECONCILIATION_AGE,
    ) -> UsageHealth:
        now = now or datetime.now(timezone.utc)
        facts = await self._reports.health_facts(
            now=now, lookback_start=now - HEALTH_LOOKBACK, grace_end=now - USAGE_REPORT_GRACE
        )
        return evaluate_usage_health(facts, max_reconciliation_age=max_reconciliation_age)
