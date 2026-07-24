from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceArea:
    id: uuid.UUID
    organization_id: uuid.UUID
    label: str
    postal_code: str | None
    city: str | None
    state: str | None
