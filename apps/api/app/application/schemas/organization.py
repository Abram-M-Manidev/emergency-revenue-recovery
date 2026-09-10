from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    voice_assistant_enabled: bool
    created_at: datetime
    updated_at: datetime


class UpdateOrganizationRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    is_active: bool | None = None
    #: The per-tenant voice kill switch. Setting this False stops the inbound
    #: phone assistant answering for this organization while leaving the
    #: dashboard, dispatch queue, appointments and customers fully usable —
    #: which is the whole point of it being separate from `is_active`.
    voice_assistant_enabled: bool | None = None
