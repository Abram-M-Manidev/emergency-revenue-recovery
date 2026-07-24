from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BusinessType(str, Enum):
    HVAC = "hvac"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    id: uuid.UUID
    organization_id: uuid.UUID
    business_type: BusinessType
    display_name: str
    phone_number: str | None
    timezone: str
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str
    website: str | None
    created_at: datetime
    updated_at: datetime
