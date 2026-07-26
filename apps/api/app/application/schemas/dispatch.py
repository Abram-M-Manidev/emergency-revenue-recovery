from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.entities.emergency_ticket import TicketStatus

# --- Technicians ---


class TechnicianProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    # None only on the on-call toggle response, which doesn't look these up
    # (the frontend already has them from the roster it fetched earlier).
    full_name: str | None = None
    email: str | None = None
    phone_number: str
    is_on_call: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class CreateTechnicianRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone_number: str = Field(min_length=1, max_length=32)
    # bcrypt (see infrastructure/security/password.py) silently truncates
    # beyond 72 bytes and this codebase rejects that instead — capping the
    # request field here keeps that a normal 422, not a raw ValueError.
    temporary_password: str = Field(min_length=8, max_length=72)


class SetOnCallRequest(BaseModel):
    is_on_call: bool


# --- Tickets ---


class EmergencyTicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    matched_service_id: uuid.UUID | None
    status: TicketStatus
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    assigned_technician_user_id: uuid.UUID | None
    assigned_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    customer_id: uuid.UUID | None = None


class AssignTicketRequest(BaseModel):
    technician_user_id: uuid.UUID


class UpdateTicketStatusRequest(BaseModel):
    status: TicketStatus
