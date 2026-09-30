"""The public acknowledgement endpoint for emergency pages.

Unauthenticated by design: the recipient of a page is often a technician on a
personal phone with no dashboard session. The signed token in the link is the
whole authority — it names one page and one recipient role, carries a 128-bit
MAC, and expires (see `infrastructure/paging/ack_links.py`). Knowing a ticket
or page id is not enough to forge one, and a link can acknowledge only the
page it was issued for.

POST, never GET: messaging apps fetch links to build previews, and a GET
that acknowledged would let a phone's link preview silence an escalation
before any person had read the page. The link opens a dashboard page whose
button makes this POST.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_emergency_paging_service
from app.application.schemas.paging import AcknowledgeLinkRequest, AcknowledgementResponse
from app.application.services.emergency_paging_service import EmergencyPagingService

router = APIRouter(prefix="/paging", tags=["paging"])


@router.post("/acknowledge", response_model=AcknowledgementResponse)
async def acknowledge_with_link(
    payload: AcknowledgeLinkRequest,
    paging: EmergencyPagingService = Depends(get_emergency_paging_service),
) -> AcknowledgementResponse:
    """Idempotent. A malformed, forged or expired token is one 404, and the
    token is never echoed back."""
    result = await paging.acknowledge_with_link(payload.token)
    return AcknowledgementResponse(
        status=result.page.status,
        acknowledged_at=result.page.acknowledged_at,
        already_acknowledged=not result.newly_acknowledged,
    )
