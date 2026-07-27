"""Organization settings endpoints (Milestone 9): view and rename the
caller's own organization, or deactivate/reactivate it. Every route derives
its organization scope from `current_user.organization_id` — never a
client-supplied id.

Gated entirely behind `organization:manage`, which only the Owner role
holds by default (Admin/Member never did, even before this milestone) —
so this stays Owner-only, matching the least-privilege default the RBAC
catalogue already encodes rather than introducing a new read-only
permission just for this milestone."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_organization_service, require_permission
from app.application.schemas.organization import OrganizationResponse, UpdateOrganizationRequest
from app.application.services.organization_service import OrganizationService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/organizations", tags=["organizations"])

_manage_user = require_permission(Permissions.ORGANIZATION_MANAGE)


@router.get("/current", response_model=OrganizationResponse)
async def get_current_organization(
    user: User = Depends(_manage_user),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationResponse:
    organization = await service.get_current(user.organization_id)
    return OrganizationResponse.model_validate(organization)


@router.patch("/current", response_model=OrganizationResponse)
async def update_current_organization(
    payload: UpdateOrganizationRequest,
    user: User = Depends(_manage_user),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationResponse:
    organization = await service.update_current(
        user.organization_id,
        name=payload.name,
        is_active=payload.is_active,
    )
    return OrganizationResponse.model_validate(organization)
