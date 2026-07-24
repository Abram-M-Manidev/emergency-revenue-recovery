from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)


class ConversationOutcomeRepository(ABC):
    """One outcome per conversation — `upsert` creates it on the first call
    and updates it in place on every subsequent one, so a unique
    (conversation_id) constraint is never raced against a plain insert."""

    @abstractmethod
    async def upsert(
        self,
        conversation_id: uuid.UUID,
        *,
        classification: CallClassification,
        confidence: float,
        recommended_action: RecommendedAction,
        matched_service_id: uuid.UUID | None,
        customer_name: str | None,
        customer_phone: str | None,
        customer_address: str | None,
        summary: str,
    ) -> ConversationOutcome: ...

    @abstractmethod
    async def get_by_conversation_id(
        self, conversation_id: uuid.UUID
    ) -> ConversationOutcome | None: ...
