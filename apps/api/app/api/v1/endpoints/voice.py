"""Admin-facing, JWT-authenticated Voice endpoints: read-only views of an
org's voice line configuration and a voice conversation's call metadata.
No write endpoints exist here — provisioning (creating the Vapi assistant,
importing the Twilio number) is an ops-side step for this milestone, so
there is nothing for an org admin to manage from the app yet. Every route
derives its organization scope from `current_user.organization_id`, same
convention as `ai_conversations.py`."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import get_voice_service, require_permission
from app.application.schemas.voice import VoiceCallResponse, VoiceLineResponse
from app.application.services.voice_service import VoiceService
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User

router = APIRouter(prefix="/voice", tags=["voice"])

_read_user = require_permission(Permissions.VOICE_READ)


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
) -> VoiceCallResponse:
    voice_call = await service.get_voice_call(user.organization_id, conversation_id)
    return VoiceCallResponse.model_validate(voice_call)
