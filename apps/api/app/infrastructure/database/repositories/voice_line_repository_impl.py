from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.repositories.voice_line_repository import VoiceLineRepository
from app.infrastructure.database.models.voice_line import VoiceLineModel


def _to_entity(model: VoiceLineModel) -> VoiceLine:
    return VoiceLine(
        id=model.id,
        organization_id=model.organization_id,
        provider=VoiceProvider(model.provider),
        vapi_assistant_id=model.vapi_assistant_id,
        vapi_phone_number_id=model.vapi_phone_number_id,
        phone_number=model.phone_number,
        is_active=model.is_active,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyVoiceLineRepository(VoiceLineRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_organization_id(self, organization_id: uuid.UUID) -> VoiceLine | None:
        result = await self._session.execute(
            select(VoiceLineModel).where(VoiceLineModel.organization_id == organization_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_vapi_assistant_id(self, assistant_id: str) -> VoiceLine | None:
        result = await self._session.execute(
            select(VoiceLineModel).where(VoiceLineModel.vapi_assistant_id == assistant_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def get_by_vapi_phone_number_id(self, phone_number_id: str) -> VoiceLine | None:
        result = await self._session.execute(
            select(VoiceLineModel).where(VoiceLineModel.vapi_phone_number_id == phone_number_id)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        provider: VoiceProvider,
        vapi_assistant_id: str,
        vapi_phone_number_id: str | None,
        phone_number: str | None,
    ) -> VoiceLine:
        model = VoiceLineModel(
            organization_id=organization_id,
            provider=provider,
            vapi_assistant_id=vapi_assistant_id,
            vapi_phone_number_id=vapi_phone_number_id,
            phone_number=phone_number,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)
