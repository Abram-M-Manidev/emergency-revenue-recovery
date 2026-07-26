"""Analytics endpoints (Milestone 8): a single read-only aggregate report
over conversation volume, ticket/appointment funnel metrics, customer
growth, and revenue recovered. Every route derives its organization scope
from `current_user.organization_id` — never a client-supplied id — same
convention as every other module.

Reading requires `analytics:read` (Owner/Admin/Member; Technician does not
hold it — see `app/domain/entities/rbac.py`). There is no manage tier:
Analytics has nothing of its own to create or edit, only to report on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_analytics_service, require_permission
from app.application.schemas.analytics import (
    AnalyticsSummaryResponse,
    BucketCountResponse,
    DailyCountResponse,
    DailyRevenueResponse,
)
from app.application.services.analytics_service import AnalyticsService
from app.domain.entities.analytics import DateRangePreset
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/analytics", tags=["analytics"])

_read_user = require_permission(Permissions.ANALYTICS_READ)


@router.get("/summary", response_model=AnalyticsSummaryResponse)
async def get_analytics_summary(
    range: DateRangePreset = Query(default=DateRangePreset.LAST_30_DAYS),
    user: User = Depends(_read_user),
    service: AnalyticsService = Depends(get_analytics_service),
) -> AnalyticsSummaryResponse:
    summary = await service.get_summary(user.organization_id, preset=range)
    return AnalyticsSummaryResponse(
        range_start=summary.range_start,
        range_end=summary.range_end,
        total_conversations=summary.total_conversations,
        conversations_by_day=[
            DailyCountResponse(day=d.day, count=d.count) for d in summary.conversations_by_day
        ],
        conversations_by_channel=[
            BucketCountResponse(label=b.label, count=b.count)
            for b in summary.conversations_by_channel
        ],
        classification_breakdown=[
            BucketCountResponse(label=b.label, count=b.count)
            for b in summary.classification_breakdown
        ],
        recommended_action_breakdown=[
            BucketCountResponse(label=b.label, count=b.count)
            for b in summary.recommended_action_breakdown
        ],
        tickets_created=summary.tickets_created,
        tickets_resolved=summary.tickets_resolved,
        average_ticket_resolution_minutes=summary.average_ticket_resolution_minutes,
        appointments_created=summary.appointments_created,
        appointments_completed=summary.appointments_completed,
        appointments_no_show=summary.appointments_no_show,
        appointment_show_up_rate=summary.appointment_show_up_rate,
        appointment_status_breakdown=[
            BucketCountResponse(label=b.label, count=b.count)
            for b in summary.appointment_status_breakdown
        ],
        new_customers=summary.new_customers,
        total_customers=summary.total_customers,
        ticket_revenue=float(summary.ticket_revenue),
        appointment_revenue=float(summary.appointment_revenue),
        total_revenue=float(summary.total_revenue),
        revenue_by_day=[
            DailyRevenueResponse(day=d.day, amount=float(d.amount)) for d in summary.revenue_by_day
        ],
    )
