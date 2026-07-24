from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.exceptions import EntityAlreadyExistsError
from app.domain.repositories.emergency_keyword_repository import EmergencyKeywordRepository
from app.infrastructure.database.models.emergency_keyword import EmergencyKeywordModel


def _to_entity(model: EmergencyKeywordModel) -> EmergencyKeyword:
    return EmergencyKeyword(
        id=model.id, organization_id=model.organization_id, phrase=model.phrase, notes=model.notes
    )


class SqlAlchemyEmergencyKeywordRepository(EmergencyKeywordRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, organization_id: uuid.UUID) -> list[EmergencyKeyword]:
        result = await self._session.execute(
            select(EmergencyKeywordModel)
            .where(EmergencyKeywordModel.organization_id == organization_id)
            .order_by(EmergencyKeywordModel.phrase)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def create(
        self, *, organization_id: uuid.UUID, phrase: str, notes: str | None
    ) -> EmergencyKeyword:
        model = EmergencyKeywordModel(organization_id=organization_id, phrase=phrase, notes=notes)
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise EntityAlreadyExistsError("EmergencyKeyword", "phrase", phrase) from exc
        await self._session.refresh(model)
        return _to_entity(model)

    async def delete(self, organization_id: uuid.UUID, keyword_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(EmergencyKeywordModel).where(
                EmergencyKeywordModel.id == keyword_id,
                EmergencyKeywordModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
