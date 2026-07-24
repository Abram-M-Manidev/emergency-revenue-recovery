from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.entities.role import Role


@dataclass(frozen=True, slots=True)
class User:
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    hashed_password: str
    full_name: str
    is_active: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    roles: tuple[Role, ...] = field(default_factory=tuple)

    @property
    def permission_codes(self) -> frozenset[str]:
        codes: set[str] = set()
        for role in self.roles:
            codes.update(role.permission_codes)
        return frozenset(codes)

    def has_permission(self, code: str) -> bool:
        return self.is_superuser or code in self.permission_codes
