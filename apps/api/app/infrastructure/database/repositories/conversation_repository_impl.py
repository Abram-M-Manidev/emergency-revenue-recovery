from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.analytics import BucketCount, DailyCount
from app.domain.entities.conversation import Conversation, ConversationChannel, ConversationStatus
from app.domain.entities.conversation_message import ConversationMessage, MessageRole
from app.domain.repositories.conversation_repository import ConversationRepository
from app.infrastructure.database.models.conversation import ConversationModel
from app.infrastructure.database.models.conversation_message import ConversationMessageModel


def _to_entity(model: ConversationModel) -> Conversation:
    return Conversation(
        id=model.id,
        organization_id=model.organization_id,
        channel=ConversationChannel(model.channel),
        status=ConversationStatus(model.status),
        caller_phone_number=model.caller_phone_number,
        started_at=model.started_at,
        ended_at=model.ended_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _message_to_entity(model: ConversationMessageModel) -> ConversationMessage:
    return ConversationMessage(
        id=model.id,
        conversation_id=model.conversation_id,
        role=MessageRole(model.role),
        content=model.content,
        created_at=model.created_at,
    )


class SqlAlchemyConversationRepository(ConversationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        channel: ConversationChannel,
        caller_phone_number: str | None,
    ) -> Conversation:
        model = ConversationModel(
            organization_id=organization_id,
            channel=channel,
            status=ConversationStatus.ACTIVE,
            caller_phone_number=caller_phone_number,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    async def get_by_id(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        result = await self._session.execute(
            select(ConversationModel).where(
                ConversationModel.id == conversation_id,
                ConversationModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[Conversation]:
        result = await self._session.execute(
            select(ConversationModel)
            .where(ConversationModel.organization_id == organization_id)
            .order_by(ConversationModel.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_entity(m) for m in result.scalars().all()]

    async def add_message(
        self, conversation_id: uuid.UUID, *, role: MessageRole, content: str
    ) -> ConversationMessage:
        model = ConversationMessageModel(
            conversation_id=conversation_id, role=role, content=content
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _message_to_entity(model)

    async def list_messages(self, conversation_id: uuid.UUID) -> list[ConversationMessage]:
        result = await self._session.execute(
            select(ConversationMessageModel)
            .where(ConversationMessageModel.conversation_id == conversation_id)
            .order_by(ConversationMessageModel.created_at)
        )
        return [_message_to_entity(m) for m in result.scalars().all()]

    async def complete(self, conversation_id: uuid.UUID) -> Conversation:
        result = await self._session.execute(
            select(ConversationModel).where(ConversationModel.id == conversation_id)
        )
        model = result.scalar_one()
        model.status = ConversationStatus.COMPLETED
        model.ended_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_entity(model)

    # --- Analytics (Milestone 8) aggregate queries ---

    async def count_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> int:
        query = select(func.count()).select_from(ConversationModel).where(
            ConversationModel.organization_id == organization_id,
            ConversationModel.started_at < end,
        )
        if start is not None:
            query = query.where(ConversationModel.started_at >= start)
        return (await self._session.execute(query)).scalar_one()

    async def count_by_day(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[DailyCount]:
        day = func.date_trunc("day", ConversationModel.started_at).label("day")
        query = (
            select(day, func.count())
            .where(
                ConversationModel.organization_id == organization_id,
                ConversationModel.started_at < end,
            )
            .group_by(day)
            .order_by(day)
        )
        if start is not None:
            query = query.where(ConversationModel.started_at >= start)
        rows = (await self._session.execute(query)).all()
        return [DailyCount(day=row[0].date(), count=row[1]) for row in rows]

    async def count_by_channel_in_range(
        self, organization_id: uuid.UUID, *, start: datetime | None, end: datetime
    ) -> list[BucketCount]:
        query = (
            select(ConversationModel.channel, func.count())
            .where(
                ConversationModel.organization_id == organization_id,
                ConversationModel.started_at < end,
            )
            .group_by(ConversationModel.channel)
        )
        if start is not None:
            query = query.where(ConversationModel.started_at >= start)
        rows = (await self._session.execute(query)).all()
        return [BucketCount(label=row[0].value, count=row[1]) for row in rows]
