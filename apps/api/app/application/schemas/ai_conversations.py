from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.domain.entities.conversation import ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import MessageRole
from app.domain.entities.conversation_outcome import CallClassification, RecommendedAction

# --- Conversations ---


class StartConversationRequest(BaseModel):
    caller_phone_number: str | None = Field(default=None, max_length=32)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: ConversationChannel
    status: ConversationStatus
    caller_phone_number: str | None
    started_at: datetime
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- Messages ---


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime
    # Additive, so existing clients are unaffected. Set on both messages of
    # a voice turn whose reply Vapi did not take in full (a transport-level
    # signal, not proof of playback). On an assistant message: never
    # delivered to the caller. On a customer message: reached ERRS but never
    # answered. Null for every ordinary message.
    interrupted_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def interrupted(self) -> bool:
        """`interrupted_at is not None`, for clients that only need to know."""
        return self.interrupted_at is not None


# --- Outcomes ---


class ConversationOutcomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    classification: CallClassification
    confidence: float
    recommended_action: RecommendedAction
    matched_service_id: uuid.UUID | None
    customer_name: str | None
    customer_phone: str | None
    customer_address: str | None
    summary: str
    updated_at: datetime


class SendMessageResult(BaseModel):
    conversation: ConversationResponse
    reply: MessageResponse
    outcome: ConversationOutcomeResponse


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]
    outcome: ConversationOutcomeResponse | None
