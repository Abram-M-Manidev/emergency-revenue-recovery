"""A single caller interaction with the AI Brain — one conversation holds
many messages and, once the AI has enough information, one outcome (see
`conversation_outcome.py`).

`channel` is `TEXT` only for now: Milestone 3 ships a text-based API so this
milestone is independently testable before Voice (Vapi/Twilio, Milestone 4)
exists. Milestone 4 adds a `VOICE` channel value and a transport adapter
that feeds real call transcripts into the same `AIBrainService` — no change
to this entity should be needed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ConversationChannel(str, Enum):
    TEXT = "text"


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class Conversation:
    id: uuid.UUID
    organization_id: uuid.UUID
    channel: ConversationChannel
    status: ConversationStatus
    caller_phone_number: str | None
    started_at: datetime
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
