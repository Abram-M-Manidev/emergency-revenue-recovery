"""AI Brain conversation endpoints: a text-based transport for the same
conversation engine Voice (Vapi/Twilio, Milestone 4) will later front with
real call transcripts. Every route derives its organization scope from
`current_user.organization_id` — never a client-supplied id — following the
same convention as `business_knowledge.py`.

Starting a conversation and sending messages requires
`ai_conversations:simulate` (Owner/Admin only) since each call invokes a
paid LLM API; reading conversation logs/outcomes only requires
`ai_conversations:read` (all roles)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_ai_brain_service, require_permission
from app.application.schemas.ai_conversations import (
    ConversationDetailResponse,
    ConversationOutcomeResponse,
    ConversationResponse,
    MessageResponse,
    SendMessageRequest,
    SendMessageResult,
    StartConversationRequest,
)
from app.application.services.ai_brain_service import AIBrainService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/ai/conversations", tags=["ai-conversations"])

_read_user = require_permission(Permissions.AI_CONVERSATIONS_READ)
_simulate_user = require_permission(Permissions.AI_CONVERSATIONS_SIMULATE)


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def start_conversation(
    payload: StartConversationRequest,
    user: User = Depends(_simulate_user),
    service: AIBrainService = Depends(get_ai_brain_service),
) -> ConversationResponse:
    conversation = await service.start_conversation(
        user.organization_id, caller_phone_number=payload.caller_phone_number
    )
    return ConversationResponse.model_validate(conversation)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_read_user),
    service: AIBrainService = Depends(get_ai_brain_service),
) -> list[ConversationResponse]:
    conversations = await service.list_conversations(user.organization_id, limit=limit, offset=offset)
    return [ConversationResponse.model_validate(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(_read_user),
    service: AIBrainService = Depends(get_ai_brain_service),
) -> ConversationDetailResponse:
    conversation = await service.get_conversation(user.organization_id, conversation_id)
    messages = await service.list_messages(user.organization_id, conversation_id)
    outcome = await service.get_outcome(user.organization_id, conversation_id)
    return ConversationDetailResponse(
        conversation=ConversationResponse.model_validate(conversation),
        messages=[MessageResponse.model_validate(m) for m in messages],
        outcome=ConversationOutcomeResponse.model_validate(outcome) if outcome else None,
    )


@router.post("/{conversation_id}/messages", response_model=SendMessageResult)
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    user: User = Depends(_simulate_user),
    service: AIBrainService = Depends(get_ai_brain_service),
) -> SendMessageResult:
    result = await service.send_message(user.organization_id, conversation_id, payload.message)
    return SendMessageResult(
        conversation=ConversationResponse.model_validate(result.conversation),
        reply=MessageResponse.model_validate(result.reply_message),
        outcome=ConversationOutcomeResponse.model_validate(result.outcome),
    )
