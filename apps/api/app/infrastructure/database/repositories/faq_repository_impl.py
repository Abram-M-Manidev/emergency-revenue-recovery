from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.faq_entry import FAQEntry
from app.domain.repositories.faq_repository import FAQRepository
from app.infrastructure.database.models.faq_entry import FAQEntryModel


def _to_entity(model: FAQEntryModel) -> FAQEntry:
    return FAQEntry(
        id=model.id,
        organization_id=model.organization_id,
        question=model.question,
        answer=model.answer,
        category=model.category,
        is_active=model.is_active,
    )


class SqlAlchemyFAQRepository(FAQRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, organization_id: uuid.UUID) -> list[FAQEntry]:
        result = await self._session.execute(
            select(FAQEntryModel)
            .where(FAQEntryModel.organization_id == organization_id)
            .order_by(FAQEntryModel.question)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry:
        model = FAQEntryModel(
            organization_id=organization_id,
            question=question,
            answer=answer,
            category=category,
            is_active=is_active,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def update(
        self,
        organization_id: uuid.UUID,
        faq_id: uuid.UUID,
        *,
        question: str,
        answer: str,
        category: str | None,
        is_active: bool,
    ) -> FAQEntry | None:
        result = await self._session.execute(
            select(FAQEntryModel).where(
                FAQEntryModel.id == faq_id, FAQEntryModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None

        model.question = question
        model.answer = answer
        model.category = category
        model.is_active = is_active

        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def delete(self, organization_id: uuid.UUID, faq_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(FAQEntryModel).where(
                FAQEntryModel.id == faq_id, FAQEntryModel.organization_id == organization_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False
        await self._session.delete(model)
        await self._session.flush()
        return True
