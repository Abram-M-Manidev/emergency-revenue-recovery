from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import date, datetime

from app.domain.usage_report import UsageHealthFacts, UsageSummary


class UsageReportRepository(ABC):
    @abstractmethod
    async def summary(self, organization_id: uuid.UUID, billing_period: date) -> UsageSummary:
        """Every figure scoped to this organization; records with no owner
        (unattributed) are never counted against anyone."""
        ...

    @abstractmethod
    async def health_facts(
        self, *, now: datetime, lookback_start: datetime, grace_end: datetime
    ) -> UsageHealthFacts:
        """Platform-wide counts for the operator. Calls that started after
        `grace_end` are not yet expected to have a usage record."""
        ...
