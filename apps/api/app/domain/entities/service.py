from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Service:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    is_emergency_eligible: bool
    is_active: bool
