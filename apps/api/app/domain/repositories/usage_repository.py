from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.ai.usage import TokenUsageTotal
from app.domain.usage import CallUsageRecord


class CallUsageRepository(ABC):
    @abstractmethod
    async def get_for_update(self, vapi_call_id: str) -> CallUsageRecord | None:
        """The stored record, row-locked for the rest of the transaction so
        two deliveries of the same report cannot interleave their merges."""
        ...

    @abstractmethod
    async def insert_if_absent(self, record: CallUsageRecord) -> bool:
        """True when this call wrote the row; False when it already existed.
        Never raises on a duplicate."""
        ...

    @abstractmethod
    async def update(self, record: CallUsageRecord) -> CallUsageRecord: ...


class LLMUsageRepository(ABC):
    @abstractmethod
    async def add(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        channel: str,
        total: TokenUsageTotal,
    ) -> None:
        """Adds to the conversation's running total for that model.
        Additive by design: a retried turn is a real second model call."""
        ...
