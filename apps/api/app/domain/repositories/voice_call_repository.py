from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.voice_call import VoiceCall


class VoiceCallRepository(ABC):
    @abstractmethod
    async def get_by_vapi_call_id(self, vapi_call_id: str) -> VoiceCall | None: ...

    @abstractmethod
    async def get_by_conversation_id(self, conversation_id: uuid.UUID) -> VoiceCall | None: ...

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        vapi_call_id: str,
        caller_number: str | None,
    ) -> VoiceCall: ...

    @abstractmethod
    async def mark_ended(
        self,
        vapi_call_id: str,
        *,
        ended_reason: str | None,
        duration_seconds: int | None,
        recording_url: str | None,
    ) -> VoiceCall:
        """No-op-safe: called from the `end-of-call-report` webhook, which
        Vapi may in principle redeliver."""
        ...
