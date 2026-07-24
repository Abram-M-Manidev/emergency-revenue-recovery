from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.faq_entry import FAQEntry


class FAQRepository(ABC):
    @abstractmethod
    async def list(self, organization_id: uuid.UUID) -> list[FAQEntry]: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry: ...

    @abstractmethod
    async def update(
        self,
        organization_id: uuid.UUID,
        faq_id: uuid.UUID,
        *,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry | None:
        """Replaces the full entity with the given (already-merged) field
        values. Returns None if no matching FAQ exists for this
        organization."""
        ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID, faq_id: uuid.UUID) -> bool:
        """Returns True if a row was deleted, False if no matching FAQ
        exists for this organization."""
        ...
