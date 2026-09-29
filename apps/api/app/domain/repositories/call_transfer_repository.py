"""Persistence ports for human fallback: per-tenant transfer settings
(configuration, written rarely by an operator) and transfer attempts (events,
one per attempt, the audit trail of what the caller was offered)."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.call_transfer.attempt import CallTransferAttempt
from app.domain.call_transfer.settings import CallTransferSettings


class CallTransferSettingsRepository(ABC):
    @abstractmethod
    async def get(self, organization_id: uuid.UUID) -> CallTransferSettings | None: ...

    @abstractmethod
    async def upsert(self, settings: CallTransferSettings) -> CallTransferSettings: ...

    @abstractmethod
    async def delete(self, organization_id: uuid.UUID) -> None: ...


class CallTransferAttemptRepository(ABC):
    @abstractmethod
    async def add(self, attempt: CallTransferAttempt) -> CallTransferAttempt: ...

    @abstractmethod
    async def save(self, attempt: CallTransferAttempt) -> CallTransferAttempt:
        """Persist a state change of an attempt already added."""
        ...

    @abstractmethod
    async def get_initiated_for_conversation(
        self, conversation_id: uuid.UUID
    ) -> CallTransferAttempt | None:
        """The transfer this call was already handed to, if any — so a model
        that asks twice never moves a call that is already moving."""
        ...

    @abstractmethod
    async def list_for_conversation(self, conversation_id: uuid.UUID) -> list[CallTransferAttempt]: ...
