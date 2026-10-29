from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.follow_ups import CallFollowUp


class CallFollowUpRepository(ABC):
    @abstractmethod
    async def add_if_absent(self, follow_up: CallFollowUp) -> CallFollowUp:
        """Records the follow-up, or returns the one this conversation
        already has. Never raises on a duplicate: a retried turn and a
        repeated end-of-call report both arrive here."""
        ...

    @abstractmethod
    async def mark_possible_emergency(
        self, follow_up_id: uuid.UUID, *, emergency_ticket_id: uuid.UUID | None
    ) -> CallFollowUp: ...

    @abstractmethod
    async def get_for_conversation(self, conversation_id: uuid.UUID) -> CallFollowUp | None: ...

    @abstractmethod
    async def list_for_organization(
        self, organization_id: uuid.UUID, *, include_resolved: bool, limit: int
    ) -> list[CallFollowUp]: ...

    @abstractmethod
    async def resolve(
        self,
        organization_id: uuid.UUID,
        follow_up_id: uuid.UUID,
        *,
        resolved_by_user_id: uuid.UUID,
        resolved_at: datetime,
    ) -> CallFollowUp | None:
        """None when no such follow-up exists in this organization."""
        ...
