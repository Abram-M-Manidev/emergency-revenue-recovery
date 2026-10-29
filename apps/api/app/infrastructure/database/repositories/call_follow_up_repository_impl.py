"""SQLAlchemy implementation of the call follow-up repository."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.follow_ups import CallFollowUp
from app.domain.repositories.call_follow_up_repository import CallFollowUpRepository
from app.infrastructure.database.models.call_follow_up import CallFollowUpModel


def _to_entity(model: CallFollowUpModel) -> CallFollowUp:
    return CallFollowUp(
        id=model.id,
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        reason=model.reason,
        possible_emergency=model.possible_emergency,
        emergency_ticket_id=model.emergency_ticket_id,
        caller_number=model.caller_number,
        resolved_at=model.resolved_at,
        resolved_by_user_id=model.resolved_by_user_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyCallFollowUpRepository(CallFollowUpRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, follow_up: CallFollowUp) -> CallFollowUp:
        # ON CONFLICT DO NOTHING on the unique conversation_id: a duplicate is
        # the normal case (Vapi retries, repeated reports), and a unique
        # violation would poison the request's transaction.
        await self._session.execute(
            pg_insert(CallFollowUpModel)
            .values(
                id=follow_up.id,
                organization_id=follow_up.organization_id,
                conversation_id=follow_up.conversation_id,
                reason=follow_up.reason,
                possible_emergency=follow_up.possible_emergency,
                emergency_ticket_id=follow_up.emergency_ticket_id,
                caller_number=follow_up.caller_number,
            )
            .on_conflict_do_nothing(index_elements=["conversation_id"])
        )
        await self._session.flush()
        stored = await self.get_for_conversation(follow_up.conversation_id)
        assert stored is not None  # just written, or already there
        return stored

    async def mark_possible_emergency(
        self, follow_up_id: uuid.UUID, *, emergency_ticket_id: uuid.UUID | None
    ) -> CallFollowUp:
        await self._session.execute(
            update(CallFollowUpModel)
            .where(CallFollowUpModel.id == follow_up_id)
            .values(
                possible_emergency=True,
                emergency_ticket_id=emergency_ticket_id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()
        self._session.expire_all()
        model = await self._session.get(CallFollowUpModel, follow_up_id)
        assert model is not None
        return _to_entity(model)

    async def get_for_conversation(self, conversation_id: uuid.UUID) -> CallFollowUp | None:
        # Bypasses the identity map: the row may have just been written by a
        # core INSERT that the session never saw as an object.
        result = await self._session.execute(
            select(CallFollowUpModel)
            .where(CallFollowUpModel.conversation_id == conversation_id)
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def list_for_organization(
        self, organization_id: uuid.UUID, *, include_resolved: bool, limit: int
    ) -> list[CallFollowUp]:
        statement = select(CallFollowUpModel).where(
            CallFollowUpModel.organization_id == organization_id
        )
        if not include_resolved:
            statement = statement.where(CallFollowUpModel.resolved_at.is_(None))
        # Possible emergencies first, then oldest first: the call waiting
        # longest for a callback is the one most likely to be going wrong.
        statement = statement.order_by(
            CallFollowUpModel.possible_emergency.desc(), CallFollowUpModel.created_at
        ).limit(limit)
        result = await self._session.execute(statement)
        return [_to_entity(m) for m in result.scalars()]

    async def resolve(
        self,
        organization_id: uuid.UUID,
        follow_up_id: uuid.UUID,
        *,
        resolved_by_user_id: uuid.UUID,
        resolved_at: datetime,
    ) -> CallFollowUp | None:
        result = await self._session.execute(
            select(CallFollowUpModel).where(
                CallFollowUpModel.id == follow_up_id,
                CallFollowUpModel.organization_id == organization_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        if model.resolved_at is None:
            # Resolving twice keeps the first resolution: who called back,
            # and when, is the record worth keeping.
            model.resolved_at = resolved_at
            model.resolved_by_user_id = resolved_by_user_id
            await self._session.flush()
            # `updated_at` is server-set on flush and so expired; reading it
            # lazily would need a greenlet this async session does not have.
            await self._session.refresh(model)
        return _to_entity(model)
