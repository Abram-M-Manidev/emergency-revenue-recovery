from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.conversation import Conversation, ConversationChannel
from app.domain.entities.conversation_message import ConversationMessage, MessageRole


class ConversationRepository(ABC):
    """Owns a conversation and its ordered messages — always read and
    appended to together as one aggregate."""

    @abstractmethod
    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        channel: ConversationChannel,
        caller_phone_number: str | None,
    ) -> Conversation: ...

    @abstractmethod
    async def get_by_id(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None: ...

    @abstractmethod
    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[Conversation]:
        """Newest-first, paginated."""
        ...

    @abstractmethod
    async def add_message(
        self, conversation_id: uuid.UUID, *, role: MessageRole, content: str
    ) -> ConversationMessage: ...

    @abstractmethod
    async def list_messages(self, conversation_id: uuid.UUID) -> list[ConversationMessage]:
        """Oldest-first."""
        ...

    @abstractmethod
    async def complete(self, conversation_id: uuid.UUID) -> Conversation:
        """Marks the conversation COMPLETED and stamps ended_at."""
        ...
