from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.service_area import ServiceArea


class ServiceAreaRepository(ABC):
    @abstractmethod
    async def list(self, organization_id: uuid.UUID) -> list[ServiceArea]: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        label: str,
        postal_code: str | None,
        city: str | None,
        state: str | None,
    ) -> ServiceArea: ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID, area_id: uuid.UUID) -> bool:
        """Returns True if a row was deleted, False if no matching area
        exists for this organization."""
        ...
