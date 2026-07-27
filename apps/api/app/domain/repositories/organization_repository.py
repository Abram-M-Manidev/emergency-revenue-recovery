from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.organization import Organization


class OrganizationRepository(ABC):
    @abstractmethod
    async def get_by_id(self, organization_id: uuid.UUID) -> Organization | None: ...

    @abstractmethod
    async def get_by_slug(self, slug: str) -> Organization | None: ...

    @abstractmethod
    async def create(self, *, name: str, slug: str) -> Organization: ...

    @abstractmethod
    async def update(
        self,
        organization_id: uuid.UUID,
        *,
        name: str | None = None,
        is_active: bool | None = None,
    ) -> Organization:
        """Partial update: only fields explicitly passed are changed."""
        ...
