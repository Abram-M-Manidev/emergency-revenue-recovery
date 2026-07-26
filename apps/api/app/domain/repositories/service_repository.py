from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from decimal import Decimal

from app.domain.entities.service import Service


class ServiceRepository(ABC):
    @abstractmethod
    async def list(self, organization_id: uuid.UUID) -> list[Service]: ...

    @abstractmethod
    async def get_by_id(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> Service | None: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
        default_duration_minutes: int | None,
        default_price: Decimal | None = None,
    ) -> Service: ...

    @abstractmethod
    async def update(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        *,
        name: str,
        description: str | None,
        category: str | None,
        is_emergency_eligible: bool,
        is_active: bool,
        default_duration_minutes: int | None,
        default_price: Decimal | None = None,
    ) -> Service | None:
        """Replaces the full entity with the given (already-merged) field
        values. Returns None if no matching service exists for this
        organization. Merging "what actually changed" from a partial update
        request is the application layer's job, not the repository's."""
        ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        """Returns True if a row was deleted, False if no matching service
        exists for this organization."""
        ...
