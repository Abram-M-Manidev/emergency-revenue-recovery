from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.appointment import AppointmentStatus


class AppointmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    matched_service_id: uuid.UUID | None
    status: AppointmentStatus
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    scheduled_start_at: datetime | None
    duration_minutes: int | None
    assigned_technician_user_id: uuid.UUID | None
    assigned_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    customer_id: uuid.UUID | None = None
    actual_value: float | None = None


class ScheduleAppointmentRequest(BaseModel):
    scheduled_start_at: datetime
    duration_minutes: int = Field(gt=0, le=1440)
    technician_user_id: uuid.UUID | None = None


class UpdateAppointmentStatusRequest(BaseModel):
    status: AppointmentStatus
    actual_value: float | None = Field(default=None, ge=0)
