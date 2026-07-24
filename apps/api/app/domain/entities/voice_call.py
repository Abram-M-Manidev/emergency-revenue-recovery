"""Voice-specific transport metadata for one phone call — provider call id,
timing, how it ended, recording. Deliberately kept out of `Conversation`
(app/domain/entities/conversation.py): that entity is channel-agnostic by
design, and every fact here (a Vapi call id, an "ended reason", a recording
URL) is meaningless for a text-channel conversation. One `VoiceCall` maps to
exactly one `Conversation`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class VoiceCall:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    vapi_call_id: str
    caller_number: str | None
    started_at: datetime
    ended_at: datetime | None
    ended_reason: str | None
    duration_seconds: int | None
    recording_url: str | None
    created_at: datetime
    updated_at: datetime
