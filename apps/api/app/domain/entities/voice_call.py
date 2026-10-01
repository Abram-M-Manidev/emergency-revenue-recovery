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
    # What this call was told about the assistant and recording, and when
    # (see `app/domain/disclosure.py`). All None for a call that predates
    # disclosure, or on which ERRS never got to speak — "unknown", which is
    # deliberately not the same as "no notice was given".
    disclosure_sent_at: datetime | None = None
    disclosed_ai: bool | None = None
    disclosed_recording: bool | None = None

    @property
    def recording_notice_missing(self) -> bool:
        """A recording exists, and ERRS knows it did NOT tell the caller the
        call was recorded. Unknown (no disclosure on record) is not
        "missing": it cannot be asserted either way."""
        return self.recording_url is not None and self.disclosed_recording is False
