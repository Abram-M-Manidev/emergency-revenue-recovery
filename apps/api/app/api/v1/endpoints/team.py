"""Team management endpoints (Milestone 9): list/invite/deactivate/re-role
the other users in the caller's own organization. Every route derives its
organization scope from `current_user.organization_id` — never a
client-supplied id — same convention as every other module.

Listing requires `users:read` (Owner/Admin/Member all hold it); inviting,
deactivating/reactivating, and changing a member's role require
`users:manage` (Owner/Admin only). `TeamService` enforces the finer-grained
business rules (can't deactivate yourself, can't demote/deactivate the
last Owner, the `Technician` role isn't assignable here) since those are
resource-specific, not static permissions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import get_team_service, require_permission
from app.application.schemas.team import (
    InviteTeamMemberRequest,
    TeamMemberResponse,
    UpdateMemberRoleRequest,
    UpdateMemberStatusRequest,
)
from app.application.services.team_service import TeamService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/team", tags=["team"])

_read_user = require_permission(Permissions.USERS_READ)
_manage_user = require_permission(Permissions.USERS_MANAGE)


def _to_response(member: User) -> TeamMemberResponse:
    return TeamMemberResponse(
        id=member.id,
        email=member.email,
        full_name=member.full_name,
        roles=[role.name for role in member.roles],
        is_active=member.is_active,
        is_superuser=member.is_superuser,
        last_login_at=member.last_login_at,
        created_at=member.created_at,
    )


@router.get("/members", response_model=list[TeamMemberResponse])
async def list_members(
    user: User = Depends(_read_user),
    service: TeamService = Depends(get_team_service),
) -> list[TeamMemberResponse]:
    members = await service.list_members(user.organization_id)
    return [_to_response(member) for member in members]


@router.post("/members", response_model=TeamMemberResponse, status_code=status.HTTP_201_CREATED)
async def invite_member(
    payload: InviteTeamMemberRequest,
    user: User = Depends(_manage_user),
    service: TeamService = Depends(get_team_service),
) -> TeamMemberResponse:
    member = await service.invite_member(
        user.organization_id,
        full_name=payload.full_name,
        email=payload.email,
        temporary_password=payload.temporary_password,
        role_name=payload.role,
    )
    return _to_response(member)


@router.patch("/members/{user_id}/status", response_model=TeamMemberResponse)
async def update_member_status(
    user_id: uuid.UUID,
    payload: UpdateMemberStatusRequest,
    user: User = Depends(_manage_user),
    service: TeamService = Depends(get_team_service),
) -> TeamMemberResponse:
    member = await service.set_member_active(
        user.organization_id,
        user_id,
        is_active=payload.is_active,
        acting_user_id=user.id,
    )
    return _to_response(member)


@router.patch("/members/{user_id}/role", response_model=TeamMemberResponse)
async def update_member_role(
    user_id: uuid.UUID,
    payload: UpdateMemberRoleRequest,
    user: User = Depends(_manage_user),
    service: TeamService = Depends(get_team_service),
) -> TeamMemberResponse:
    member = await service.set_member_role(
        user.organization_id,
        user_id,
        role_name=payload.role,
        acting_user_id=user.id,
    )
    return _to_response(member)
