"""Appointment Management endpoints: the appointment queue staff use to turn
AI-Brain-requested appointments into scheduled, technician-assigned jobs.
Every route derives its organization scope from `current_user.organization_id`
— never a client-supplied id — same convention as every other module.

Reading and updating your own assigned appointment's status only requires
`appointments:read` (Owner/Admin/Member/Technician all hold it);
`AppointmentService` does the finer-grained "own appointment or
appointments:manage" check on status updates, since that's a business rule
about a specific resource, not a static permission (see its docstring, and
`dispatch.py`'s identical reasoning for tickets). Scheduling/rescheduling an
appointment requires `appointments:manage` (Owner/Admin only)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_appointment_service, require_permission
from app.application.schemas.appointment import (
    AppointmentResponse,
    ScheduleAppointmentRequest,
    UpdateAppointmentStatusRequest,
)
from app.application.services.appointment_service import AppointmentService
from app.domain.entities.appointment import AppointmentStatus
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/appointments", tags=["appointments"])

_read_user = require_permission(Permissions.APPOINTMENTS_READ)
_manage_user = require_permission(Permissions.APPOINTMENTS_MANAGE)


@router.get("", response_model=list[AppointmentResponse])
async def list_appointments(
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_read_user),
    service: AppointmentService = Depends(get_appointment_service),
) -> list[AppointmentResponse]:
    appointments = await service.list_appointments(
        user.organization_id, status=status_filter, limit=limit, offset=offset
    )
    return [AppointmentResponse.model_validate(a) for a in appointments]


@router.get("/{appointment_id}", response_model=AppointmentResponse)
async def get_appointment(
    appointment_id: uuid.UUID,
    user: User = Depends(_read_user),
    service: AppointmentService = Depends(get_appointment_service),
) -> AppointmentResponse:
    appointment = await service.get_appointment(user.organization_id, appointment_id)
    return AppointmentResponse.model_validate(appointment)


@router.post("/{appointment_id}/schedule", response_model=AppointmentResponse)
async def schedule_appointment(
    appointment_id: uuid.UUID,
    payload: ScheduleAppointmentRequest,
    user: User = Depends(_manage_user),
    service: AppointmentService = Depends(get_appointment_service),
) -> AppointmentResponse:
    appointment = await service.schedule_appointment(
        user.organization_id,
        appointment_id,
        scheduled_start_at=payload.scheduled_start_at,
        duration_minutes=payload.duration_minutes,
        technician_user_id=payload.technician_user_id,
    )
    return AppointmentResponse.model_validate(appointment)


@router.post("/{appointment_id}/status", response_model=AppointmentResponse)
async def update_appointment_status(
    appointment_id: uuid.UUID,
    payload: UpdateAppointmentStatusRequest,
    user: User = Depends(_read_user),
    service: AppointmentService = Depends(get_appointment_service),
) -> AppointmentResponse:
    appointment = await service.update_appointment_status(
        user.organization_id,
        appointment_id,
        payload.status,
        acting_user=user,
        actual_value=Decimal(str(payload.actual_value)) if payload.actual_value is not None else None,
    )
    return AppointmentResponse.model_validate(appointment)
