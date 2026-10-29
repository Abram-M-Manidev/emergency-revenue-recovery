"""Admin-facing, JWT-authenticated Voice endpoints: read-only views of an
org's voice line configuration and a voice conversation's call metadata,
plus the follow-up list — calls that ended without their request being
recorded — and marking one as dealt with. Provisioning (creating the Vapi
assistant, importing the Twilio number) is still an ops-side step. Every
route derives its organization scope from `current_user.organization_id`,
same convention as `ai_conversations.py`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_call_follow_up_service,
    get_call_transfer_service,
    get_voice_service,
    require_permission,
)
from app.application.schemas.voice import (
    CallFollowUpResponse,
    CallTransferAttemptResponse,
    VoiceCallResponse,
    VoiceLineResponse,
)
from app.application.services.call_follow_up_service import CallFollowUpService
from app.application.services.voice_service import VoiceService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.infrastructure.database.session import get_db

router = APIRouter(prefix="/voice", tags=["voice"])

_read_user = require_permission(Permissions.VOICE_READ)
# Marking a callback done is dispatch work, held by the same roles that run
# the emergency queue — no new permission, so no RBAC backfill.
_resolve_user = require_permission(Permissions.DISPATCH_MANAGE)


@router.get("/line", response_model=VoiceLineResponse | None)
async def get_voice_line(
    user: User = Depends(_read_user),
    service: VoiceService = Depends(get_voice_service),
) -> VoiceLineResponse | None:
    voice_line = await service.get_voice_line(user.organization_id)
    return VoiceLineResponse.model_validate(voice_line) if voice_line else None


@router.get("/calls/{conversation_id}", response_model=VoiceCallResponse)
async def get_voice_call(
    conversation_id: uuid.UUID,
    user: User = Depends(_read_user),
    service: VoiceService = Depends(get_voice_service),
    follow_ups: CallFollowUpService = Depends(get_call_follow_up_service),
    db: AsyncSession = Depends(get_db),
) -> VoiceCallResponse:
    voice_call = await service.get_voice_call(user.organization_id, conversation_id)
    attempts = await get_call_transfer_service(db).list_for_conversation(
        user.organization_id, conversation_id
    )
    follow_up = await follow_ups.get_for_conversation(user.organization_id, conversation_id)
    response = VoiceCallResponse.model_validate(voice_call)
    response.transfer_attempts = [CallTransferAttemptResponse.from_attempt(a) for a in attempts]
    response.follow_up = CallFollowUpResponse.model_validate(follow_up) if follow_up else None
    return response


@router.get("/follow-ups", response_model=list[CallFollowUpResponse])
async def list_follow_ups(
    include_resolved: bool = Query(default=False),
    user: User = Depends(_read_user),
    follow_ups: CallFollowUpService = Depends(get_call_follow_up_service),
) -> list[CallFollowUpResponse]:
    """Calls waiting for a callback: possible emergencies first, then the
    longest-waiting."""
    items = await follow_ups.list_follow_ups(
        user.organization_id, include_resolved=include_resolved
    )
    return [CallFollowUpResponse.model_validate(item) for item in items]


@router.post("/follow-ups/{follow_up_id}/resolve", response_model=CallFollowUpResponse)
async def resolve_follow_up(
    follow_up_id: uuid.UUID,
    user: User = Depends(_resolve_user),
    follow_ups: CallFollowUpService = Depends(get_call_follow_up_service),
) -> CallFollowUpResponse:
    resolved = await follow_ups.resolve(user.organization_id, follow_up_id, user_id=user.id)
    return CallFollowUpResponse.model_validate(resolved)
