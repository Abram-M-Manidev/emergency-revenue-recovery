"""A single caller interaction with the AI Brain — one conversation holds
many messages and, once the AI has enough information, one outcome (see
`conversation_outcome.py`).

`channel` was `TEXT` only through Milestone 3 (a text-based API so that
milestone was independently testable before Voice existed). Milestone 4
adds `VOICE`: a transport adapter (see `app/application/services/
voice_service.py`) feeds real call transcripts into the same
`AIBrainService` — no other change to this entity was needed, as
anticipated. Voice-specific metadata (call id, recording, duration) lives
in `VoiceCall` (`app/domain/entities/voice_call.py`), not here — this
entity stays channel-agnostic."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ConversationChannel(str, Enum):
    TEXT = "text"
    VOICE = "voice"


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
