from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.role import Role


class RoleRepository(ABC):
    @abstractmethod
    async def seed_default_roles(self, organization_id: uuid.UUID) -> dict[str, Role]:
        """Create the standard role set (Owner/Admin/Member) for a new organization.

        Returns a mapping of role name -> created Role so callers can assign
        the owner role to the registering user without a second lookup.
        """
        ...

    @abstractmethod
    async def get_by_ids(self, role_ids: list[uuid.UUID]) -> list[Role]: ...

    @abstractmethod
    async def get_or_create_by_name(
        self, organization_id: uuid.UUID, name: str, permission_codes: tuple[str, ...]
    ) -> Role:
        """Lazily ensures a role exists for an organization that predates it
        (e.g. `Technician`, added after Milestones 1-4 already seeded
        Owner/Admin/Member for existing orgs via `seed_default_roles`).
        Idempotent: if the role already exists, its current permission set is
        returned as-is and `permission_codes` is ignored.
        """
        ...
