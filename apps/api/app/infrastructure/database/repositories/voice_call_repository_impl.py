from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.voice_call import VoiceCall
from app.domain.repositories.voice_call_repository import VoiceCallRepository
from app.infrastructure.database.models.voice_call import VoiceCallModel


def _to_entity(model: VoiceCallModel) -> VoiceCall:
    return VoiceCall(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        vapi_call_id=model.vapi_call_id,
        caller_number=model.caller_number,
        started_at=model.started_at,
        ended_at=model.ended_at,
        ended_reason=model.ended_reason,
        duration_seconds=model.duration_seconds,
        recording_url=model.recording_url,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyVoiceCallRepository(VoiceCallRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_vapi_call_id(self, vapi_call_id: str) -> VoiceCall | None:
        result = await self._session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == vapi_call_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_conversation_id(self, conversation_id: uuid.UUID) -> VoiceCall | None:
        result = await self._session.execute(
            select(VoiceCallModel).where(VoiceCallModel.conversation_id == conversation_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        vapi_call_id: str,
        caller_number: str | None,
    ) -> VoiceCall:
        model = VoiceCallModel(
            organization_id=organization_id,
            conversation_id=conversation_id,
            vapi_call_id=vapi_call_id,
            caller_number=caller_number,
            started_at=datetime.now(timezone.utc),
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def mark_ended(
        self,
        vapi_call_id: str,
        *,
        ended_reason: str | None,
        duration_seconds: int | None,
        recording_url: str | None,
    ) -> VoiceCall:
        result = await self._session.execute(
            select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == vapi_call_id)
        )
        model = result.scalar_one()
        if model.ended_at is None:
            model.ended_at = datetime.now(timezone.utc)
        model.ended_reason = ended_reason
        model.duration_seconds = duration_seconds
        model.recording_url = recording_url
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)
