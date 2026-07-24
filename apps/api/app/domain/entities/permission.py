from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Permission:
    id: uuid.UUID
    code: str
    description: str | None
