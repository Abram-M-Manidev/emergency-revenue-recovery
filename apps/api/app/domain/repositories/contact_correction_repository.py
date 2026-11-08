"""Caller corrections that need the caller's confirmation before they land.

Moving a visit to a different service address sends a technician somewhere
else, so it is the one correction the backend will not take on the model's
word alone. It works like appointment consent (`offered_slot_repository.py`):

1. The first attempt to change the address on an existing request is
   recorded here as a PROPOSAL at that turn, and refused, so the assistant
   reads the new address back.
2. Only on a LATER turn, after the caller has had a chance to answer, and
   only if the read-back reply actually reached them, may the same address be
   committed with `confirmed_correction`.

A proposal made and confirmed in one breath, or confirmed after a read-back
the caller never heard, is refused. Every proposal stays on record — with
when it was confirmed or superseded — as the audit trail of the change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

#: The one field whose change needs a confirmed read-back.
SERVICE_ADDRESS = "service_address"


@dataclass(frozen=True, slots=True)
class ContactCorrectionRequest:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    field: str
    proposed_value: str
    proposed_turn_index: int
    confirmed_at: datetime | None
    superseded_at: datetime | None
    created_at: datetime

    @property
    def is_pending(self) -> bool:
        return self.confirmed_at is None and self.superseded_at is None


class ContactCorrectionRepository(ABC):
    @abstractmethod
    async def propose(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        field: str,
        proposed_value: str,
        turn_index: int,
    ) -> ContactCorrectionRequest:
        """Records a new proposal for `field`, superseding any still pending."""
        ...

    @abstractmethod
    async def get_pending(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID, *, field: str
    ) -> ContactCorrectionRequest | None: ...

    @abstractmethod
    async def mark_confirmed(
        self, organization_id: uuid.UUID, request_id: uuid.UUID, *, at: datetime
    ) -> None: ...
