"""Shared read-side value objects for Milestone 8 (Analytics). None of these
are persisted — they are the return shapes of the aggregate query methods
added to the existing repositories owned by other modules (Conversation/
ConversationOutcome from AI Brain, EmergencyTicket from Dispatch,
Appointment from Appointments, Customer from CRM). Kept together here
because `DailyCount`/`DailyRevenue`/`BucketCount` are generic enough to be
reused across 4+ repositories, the same way `business_hours.py` holds more
than one related dataclass rather than splitting each into its own file.

`AnalyticsSummary` — the composed report `AnalyticsService.get_summary`
returns — deliberately does NOT live here: it aggregates data owned by
five other modules and is never persisted, so it belongs in
`application/services/analytics_service.py`, the same place
`CustomerService`'s `CustomerHistory` lives for the identical reason."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class DateRangePreset(str, Enum):
    TODAY = "today"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_90_DAYS = "90d"
    ALL_TIME = "all"


@dataclass(frozen=True, slots=True)
class DailyCount:
    day: date
    count: int


@dataclass(frozen=True, slots=True)
class DailyRevenue:
    day: date
    amount: Decimal


@dataclass(frozen=True, slots=True)
class BucketCount:
    label: str
    count: int
