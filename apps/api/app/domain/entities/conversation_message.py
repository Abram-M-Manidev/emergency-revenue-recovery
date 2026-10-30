from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MessageRole(str, Enum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime
    # Set on both messages of a turn whose reply Vapi did not take in full
    # (the stream was dropped before `[DONE]`: a transport signal, not proof
    # of playback). On the reply it means "not delivered", and the reply is
    # left out of the model's history. On the caller message it means "these
    # words reached ERRS but were never answered". They stay in the history
    # unless the caller's next message repeats them (see
    # `app/domain/turn_delivery.py`).
    interrupted_at: datetime | None = None
