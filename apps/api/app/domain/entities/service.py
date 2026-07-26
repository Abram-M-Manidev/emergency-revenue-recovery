from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Service:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    is_emergency_eligible: bool
    is_active: bool
    default_duration_minutes: int | None
    default_price: Decimal | None = None
