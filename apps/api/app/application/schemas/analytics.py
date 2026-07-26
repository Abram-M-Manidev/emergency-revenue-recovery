from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.domain.entities.analytics import DateRangePreset

__all__ = [
    "DateRangePreset",
    "DailyCountResponse",
    "DailyRevenueResponse",
    "BucketCountResponse",
    "AnalyticsSummaryResponse",
]


class DailyCountResponse(BaseModel):
    day: date
    count: int


class DailyRevenueResponse(BaseModel):
    day: date
    # Decimal -> float here deliberately: Pydantic v2 serializes a bare
    # `Decimal` field to a JSON *string*, not a number. Typing this `float`
    # lets `from_attributes`/manual construction coerce the domain
    # `Decimal` for us, keeping the wire format a plain number the frontend
    # can bind to directly (see `AnalyticsSummary` in analytics_service.py).
    amount: float


class BucketCountResponse(BaseModel):
    label: str
    count: int


class AnalyticsSummaryResponse(BaseModel):
    range_start: datetime | None
    range_end: datetime
    total_conversations: int
    conversations_by_day: list[DailyCountResponse]
    conversations_by_channel: list[BucketCountResponse]
    classification_breakdown: list[BucketCountResponse]
    recommended_action_breakdown: list[BucketCountResponse]
    tickets_created: int
    tickets_resolved: int
    average_ticket_resolution_minutes: float | None
    appointments_created: int
    appointments_completed: int
    appointments_no_show: int
    appointment_show_up_rate: float | None
    appointment_status_breakdown: list[BucketCountResponse]
    new_customers: int
    total_customers: int
    ticket_revenue: float
    appointment_revenue: float
    total_revenue: float
    revenue_by_day: list[DailyRevenueResponse]
