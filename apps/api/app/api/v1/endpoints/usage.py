"""Usage reporting (metering M3): what a business's calls used in a billing
period. Read-only, and scoped to `current_user.organization_id` — never a
client-supplied id — like every other module. A report, not a bill: nothing
here charges anyone."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_usage_report_service, require_permission
from app.application.schemas.usage import UsageSummaryResponse
from app.application.services.usage_report_service import (
    UsageReportService,
    current_billing_period,
)
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/usage", tags=["usage"])

_read_user = require_permission(Permissions.ANALYTICS_READ)


def _parse_period(period: str | None) -> date:
    if period is None:
        return current_billing_period()
    try:
        return datetime.strptime(period, "%Y-%m").date()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="period must be YYYY-MM",
        ) from None


@router.get("/summary", response_model=UsageSummaryResponse)
async def usage_summary(
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    current_user: User = Depends(_read_user),
    service: UsageReportService = Depends(get_usage_report_service),
) -> UsageSummaryResponse:
    summary = await service.summary(current_user.organization_id, _parse_period(period))
    return UsageSummaryResponse.from_entity(summary)
