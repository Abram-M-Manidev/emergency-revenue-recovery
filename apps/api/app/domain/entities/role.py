from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Role:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    is_system_role: bool
    permission_codes: frozenset[str] = field(default_factory=frozenset)

    def has_permission(self, code: str) -> bool:
        return code in self.permission_codes
