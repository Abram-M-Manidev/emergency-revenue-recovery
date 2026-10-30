from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime

from app.domain.ai.usage import TokenUsageTotal
from app.domain.usage import CallUsageRecord
from app.domain.usage_reconciliation import CallReconciliation, ReconciliationRun


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


class UsageReconciliationRepository(ABC):
    @abstractmethod
    async def try_lock(self) -> bool:
        """Takes the single, transaction-scoped reconciliation lock without
        waiting. False when another run holds it."""
        ...

    @abstractmethod
    async def local_call_ids_in_window(self, start: datetime, end: datetime) -> set[str]:
        """Vapi call ids of the usage records whose call falls in [start,
        end): by start time, else end time, else first receipt."""
        ...

    @abstractmethod
    async def save_run(self, run: ReconciliationRun) -> None:
        """Inserts the run, or updates it when it already exists."""
        ...

    @abstractmethod
    async def upsert(self, result: CallReconciliation) -> None:
        """One row per Vapi call: inserted, or replaced by the latest check
        (`check_count` incremented). A result without a Vapi snapshot keeps
        the stored one."""
        ...
