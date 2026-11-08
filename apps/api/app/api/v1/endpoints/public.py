"""Unauthenticated endpoints for the public marketing site.

Rate limited by `RateLimitMiddleware`'s strict `public` tier (per client IP)
and bounded by `MaxBodySizeMiddleware` like every route. The response never
depends on whether the address was already on the list."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import get_lead_service
from app.application.schemas.lead import EarlyAccessRequest, EarlyAccessResponse
from app.application.services.lead_service import LeadService

router = APIRouter(prefix="/public", tags=["public"])


@router.post(
    "/early-access",
    response_model=EarlyAccessResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_early_access(
    payload: EarlyAccessRequest,
    service: LeadService = Depends(get_lead_service),
) -> EarlyAccessResponse:
    await service.request_early_access(
        name=payload.name,
        business_name=payload.business_name,
        email=str(payload.email),
        business_type=payload.business_type,
        consent_version=payload.consent_version,
        honeypot=payload.website,
    )
    return EarlyAccessResponse()
