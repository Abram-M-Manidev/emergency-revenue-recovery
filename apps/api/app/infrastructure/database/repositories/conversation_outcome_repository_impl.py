from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.conversation_outcome import (
    CallClassification,
    ConversationOutcome,
    RecommendedAction,
)
from app.domain.repositories.conversation_outcome_repository import ConversationOutcomeRepository
from app.infrastructure.database.models.conversation_outcome import ConversationOutcomeModel


def _to_entity(model: ConversationOutcomeModel) -> ConversationOutcome:
    return ConversationOutcome(
        id=model.id,
        conversation_id=model.conversation_id,
        classification=CallClassification(model.classification),
        confidence=model.confidence,
        recommended_action=RecommendedAction(model.recommended_action),
        matched_service_id=model.matched_service_id,
        customer_name=model.customer_name,
        customer_phone=model.customer_phone,
        customer_address=model.customer_address,
        summary=model.summary,
        updated_at=model.updated_at,
    )


class SqlAlchemyConversationOutcomeRepository(ConversationOutcomeRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        conversation_id: uuid.UUID,
        *,
        classification: CallClassification,
        confidence: float,
        recommended_action: RecommendedAction,
        matched_service_id: uuid.UUID | None,
        customer_name: str | None,
        customer_phone: str | None,
        customer_address: str | None,
        summary: str,
    ) -> ConversationOutcome:
        result = await self._session.execute(
            select(ConversationOutcomeModel).where(
                ConversationOutcomeModel.conversation_id == conversation_id
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = ConversationOutcomeModel(conversation_id=conversation_id)
            self._session.add(model)

        model.classification = classification
        model.confidence = confidence
        model.recommended_action = recommended_action
        model.matched_service_id = matched_service_id
        model.customer_name = customer_name
        model.customer_phone = customer_phone
        model.customer_address = customer_address
        model.summary = summary

        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def get_by_conversation_id(
        self, conversation_id: uuid.UUID
    ) -> ConversationOutcome | None:
        result = await self._session.execute(
            select(ConversationOutcomeModel).where(
                ConversationOutcomeModel.conversation_id == conversation_id
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None
