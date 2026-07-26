from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.application.schemas.appointment import AppointmentResponse
from app.application.schemas.dispatch import EmergencyTicketResponse


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str | None
    phone_number: str
    email: str | None
    address: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class CustomerHistoryResponse(BaseModel):
    customer: CustomerResponse
    tickets: list[EmergencyTicketResponse]
    appointments: list[AppointmentResponse]


class CreateCustomerRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    phone_number: str = Field(min_length=1, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = None


class UpdateCustomerRequest(BaseModel):
    full_name: str | None = Field(default=None, max_length=255)
    phone_number: str = Field(min_length=1, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    address: str | None = Field(default=None, max_length=500)
    notes: str | None = None
