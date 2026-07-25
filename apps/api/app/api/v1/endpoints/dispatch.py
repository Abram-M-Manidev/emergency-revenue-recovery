"""Emergency Dispatch endpoints: the ticket queue and technician roster.
Every route derives its organization scope from `current_user.organization_id`
— never a client-supplied id — same convention as every other module.

Reading and updating your own assigned ticket's status only requires
`dispatch:read` (Owner/Admin/Member/Technician all hold it); `DispatchService`
does the finer-grained "own ticket or dispatch:manage" check on status
updates, since that's a business rule about a specific resource, not a
static permission (see its docstring). Everything else that changes the
ticket queue or technician roster requires `dispatch:manage`
(Owner/Admin only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_dispatch_service, require_permission
from app.application.schemas.dispatch import (
    AssignTicketRequest,
    CreateTechnicianRequest,
    EmergencyTicketResponse,
    SetOnCallRequest,
    TechnicianProfileResponse,
    UpdateTicketStatusRequest,
)
from app.application.services.dispatch_service import DispatchService
from app.domain.entities.emergency_ticket import TicketStatus
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/dispatch", tags=["dispatch"])

_read_user = require_permission(Permissions.DISPATCH_READ)
_manage_user = require_permission(Permissions.DISPATCH_MANAGE)


@router.get("/tickets", response_model=list[EmergencyTicketResponse])
async def list_tickets(
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_read_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> list[EmergencyTicketResponse]:
    tickets = await service.list_tickets(
        user.organization_id, status=status_filter, limit=limit, offset=offset
    )
    return [EmergencyTicketResponse.model_validate(t) for t in tickets]


@router.get("/tickets/{ticket_id}", response_model=EmergencyTicketResponse)
async def get_ticket(
    ticket_id: uuid.UUID,
    user: User = Depends(_read_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> EmergencyTicketResponse:
    ticket = await service.get_ticket(user.organization_id, ticket_id)
    return EmergencyTicketResponse.model_validate(ticket)


@router.post("/tickets/{ticket_id}/assign", response_model=EmergencyTicketResponse)
async def assign_ticket(
    ticket_id: uuid.UUID,
    payload: AssignTicketRequest,
    user: User = Depends(_manage_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> EmergencyTicketResponse:
    ticket = await service.assign_ticket(
        user.organization_id, ticket_id, payload.technician_user_id
    )
    return EmergencyTicketResponse.model_validate(ticket)


@router.post("/tickets/{ticket_id}/status", response_model=EmergencyTicketResponse)
async def update_ticket_status(
    ticket_id: uuid.UUID,
    payload: UpdateTicketStatusRequest,
    user: User = Depends(_read_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> EmergencyTicketResponse:
    ticket = await service.update_ticket_status(
        user.organization_id, ticket_id, payload.status, acting_user=user
    )
    return EmergencyTicketResponse.model_validate(ticket)


@router.get("/technicians", response_model=list[TechnicianProfileResponse])
async def list_technicians(
    on_call_only: bool = Query(default=False),
    user: User = Depends(_read_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> list[TechnicianProfileResponse]:
    roster = await service.list_technician_roster(user.organization_id, on_call_only=on_call_only)
    return [
        TechnicianProfileResponse(
            id=entry.profile.id,
            user_id=entry.profile.user_id,
            full_name=entry.full_name,
            email=entry.email,
            phone_number=entry.profile.phone_number,
            is_on_call=entry.profile.is_on_call,
            notes=entry.profile.notes,
            created_at=entry.profile.created_at,
            updated_at=entry.profile.updated_at,
        )
        for entry in roster
    ]


@router.post(
    "/technicians", response_model=TechnicianProfileResponse, status_code=status.HTTP_201_CREATED
)
async def create_technician(
    payload: CreateTechnicianRequest,
    user: User = Depends(_manage_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> TechnicianProfileResponse:
    technician = await service.create_technician(
        user.organization_id,
        full_name=payload.full_name,
        email=payload.email,
        phone_number=payload.phone_number,
        temporary_password=payload.temporary_password,
    )
    return TechnicianProfileResponse(
        id=technician.id,
        user_id=technician.user_id,
        full_name=payload.full_name,
        email=payload.email,
        phone_number=technician.phone_number,
        is_on_call=technician.is_on_call,
        notes=technician.notes,
        created_at=technician.created_at,
        updated_at=technician.updated_at,
    )


@router.patch("/technicians/{user_id}/on-call", response_model=TechnicianProfileResponse)
async def set_technician_on_call(
    user_id: uuid.UUID,
    payload: SetOnCallRequest,
    user: User = Depends(_manage_user),
    service: DispatchService = Depends(get_dispatch_service),
) -> TechnicianProfileResponse:
    technician = await service.set_technician_on_call(
        user.organization_id, user_id, payload.is_on_call
    )
    return TechnicianProfileResponse.model_validate(technician)
