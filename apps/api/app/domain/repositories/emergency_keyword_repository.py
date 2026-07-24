from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.emergency_keyword import EmergencyKeyword


class EmergencyKeywordRepository(ABC):
    @abstractmethod
    async def list(self, organization_id: uuid.UUID) -> list[EmergencyKeyword]: ...

    @abstractmethod
    async def create(
        self, *, organization_id: uuid.UUID, phrase: str, notes: str | None
    ) -> EmergencyKeyword: ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID, keyword_id: uuid.UUID) -> bool:
        """Returns True if a row was deleted, False if no matching keyword
        exists for this organization."""
        ...
