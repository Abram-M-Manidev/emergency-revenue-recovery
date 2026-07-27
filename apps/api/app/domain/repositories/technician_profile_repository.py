from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.technician_profile import TechnicianProfile


class TechnicianProfileRepository(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        phone_number: str,
        is_on_call: bool = True,
        notes: str | None = None,
    ) -> TechnicianProfile: ...

    @abstractmethod
    async def get_by_user_id(self, user_id: uuid.UUID) -> TechnicianProfile | None: ...

    @abstractmethod
    async def list_for_organization(
        self, organization_id: uuid.UUID, *, on_call_only: bool = False
    ) -> list[TechnicianProfile]: ...

    @abstractmethod
    async def set_on_call(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, is_on_call: bool
    ) -> TechnicianProfile:
        """`organization_id` scopes the lookup itself (Milestone 9
        tenant-isolation hardening)."""
        ...
