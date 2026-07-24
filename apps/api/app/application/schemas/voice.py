"""Two very different kinds of client hit the Voice endpoints, so this file
has two very different kinds of schema:

- Admin-facing (`VoiceLineResponse`, `VoiceCallResponse`): normal
  `from_attributes` response models, same convention as every other
  schema in this codebase.
- Vapi-facing (`Vapi*`): loosely typed on purpose. Vapi's custom-LLM
  payload carries many fields we don't use (temperature, tools, ...) and
  we don't want a schema break if Vapi's API adds more — every model here
  uses `extra="ignore"` and only pulls out what `VoiceService` needs."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.voice_line import VoiceProvider

# --- Admin-facing (JWT-authenticated) ---


class VoiceLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: VoiceProvider
    phone_number: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class VoiceCallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    caller_number: str | None
    started_at: datetime
    ended_at: datetime | None
    ended_reason: str | None
    duration_seconds: int | None
    recording_url: str | None


# --- Vapi-facing (shared-secret authenticated) ---


class VapiChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | None = None


class VapiCustomer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: str | None = None


class VapiCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    assistantId: str | None = None
    phoneNumberId: str | None = None
    customer: VapiCustomer | None = None


class VapiChatCompletionRequest(BaseModel):
    """The request Vapi sends to a Custom-LLM `model.url` for every customer
    turn. Vapi resends the full transcript each time — the last `role="user"`
    message is the new utterance; everything before it is redundant with
    what `VoiceService` already has stored (see its docstring)."""

    model_config = ConfigDict(extra="ignore")

    call: VapiCall
    messages: list[VapiChatMessage] = Field(default_factory=list)
    # Some Vapi API versions place these at the top level instead of (or in
    # addition to) nested under `call` — accepted here as a fallback so a
    # minor payload-shape difference doesn't hard-fail the webhook.
    assistantId: str | None = None
    phoneNumberId: str | None = None
