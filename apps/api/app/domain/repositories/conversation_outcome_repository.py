from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.entities.analytics import BucketCount
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

    # --- Analytics (Milestone 8) aggregate queries ---
    #
    # `ConversationOutcomeModel` carries neither `organization_id` nor
    # `created_at` of its own (confirmed against
    # infrastructure/database/models/conversation_outcome.py) — it is only
    # ever scoped through its `conversation_id` FK. Implementations must
    # join to `conversations` to filter by organization and by the parent
    # conversation's `started_at`.

    @abstractmethod
    async def classification_breakdown(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[BucketCount]:
        """Outcomes for conversations started in the range, grouped by
        `classification`."""
        ...

    @abstractmethod
    async def recommended_action_breakdown(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[BucketCount]:
        """Outcomes for conversations started in the range, grouped by
        `recommended_action`."""
        ...
