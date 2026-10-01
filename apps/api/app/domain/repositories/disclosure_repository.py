"""Persistence port for a business's caller-disclosure policy (see
`app/domain/disclosure.py`). One row per tenant; no row means the default
policy (both notices on)."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.disclosure import DisclosurePolicy, DisclosureSettings


class DisclosureSettingsRepository(ABC):
    @abstractmethod
    async def get(self, organization_id: uuid.UUID) -> DisclosureSettings | None: ...

    @abstractmethod
    async def upsert(
        self, organization_id: uuid.UUID, policy: DisclosurePolicy
    ) -> DisclosureSettings: ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID) -> None: ...
