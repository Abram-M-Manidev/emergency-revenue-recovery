from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.voice_line import VoiceLine, VoiceProvider


class VoiceLineRepository(ABC):
    """One voice line per organization for this milestone (see `VoiceLine`'s
    docstring)."""

    @abstractmethod
    async def get_by_organization_id(self, organization_id: uuid.UUID) -> VoiceLine | None: ...

    @abstractmethod
    async def get_by_vapi_assistant_id(self, assistant_id: str) -> VoiceLine | None: ...

    @abstractmethod
    async def get_by_vapi_phone_number_id(self, phone_number_id: str) -> VoiceLine | None: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        provider: VoiceProvider,
        vapi_assistant_id: str,
        vapi_phone_number_id: str | None,
        phone_number: str | None,
    ) -> VoiceLine: ...
