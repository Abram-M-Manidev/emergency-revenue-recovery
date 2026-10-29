"""A call that needs a person to call back: the record of what ERRS could not
finish on the call itself.

Every other record a call produces — a ticket, an appointment request — is
created by the assistant's own tools, so it exists only if the conversation
got far enough for the model to call them. Three things stop that, and each
used to leave nothing behind but a transcript nobody was told about (B2/B3,
2026-10-28 acceptance audit):

- the AI provider failed, so no tool could run at all;
- the caller hung up (or the line dropped) before the request was recorded;
- the call was handed to a person, and nothing records whether anyone
  answered or took the details.

One follow-up per conversation. It is created by fixed rules, never by the
model, and it is an honest claim: "this call ended without its request being
recorded". When the caller's own words described a possible emergency, the
follow-up points at the emergency ticket opened for it, so the business's
emergency alert and on-call paging run exactly as for any other emergency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class FollowUpReason(str, Enum):
    AI_UNAVAILABLE = "ai_unavailable"
    ENDED_BEFORE_REQUEST_RECORDED = "ended_before_request_recorded"
    TRANSFERRED_BEFORE_REQUEST_RECORDED = "transferred_before_request_recorded"


@dataclass(frozen=True, slots=True)
class CallFollowUp:
    id: uuid.UUID
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    reason: FollowUpReason
    #: The caller's own words matched a life-safety hazard or one of the
    #: business's emergency keywords. A routing hint, generous on purpose —
    #: not a diagnosis.
    possible_emergency: bool
    emergency_ticket_id: uuid.UUID | None
    caller_number: str | None
    resolved_at: datetime | None = None
    resolved_by_user_id: uuid.UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None
