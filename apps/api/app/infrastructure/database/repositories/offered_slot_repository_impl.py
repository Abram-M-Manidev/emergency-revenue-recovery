from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities.availability import AvailabilitySlot
from app.domain.entities.offered_slot import OfferedSlot
from app.domain.repositories.offered_slot_repository import OfferedSlotRepository
from app.infrastructure.database.models.offered_slot import OfferedSlotModel


class SqlAlchemyOfferedSlotRepository(OfferedSlotRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_offered(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        slots: Sequence[AvailabilitySlot],
        turn_index: int,
    ) -> None:
        if not slots:
            return

        # ON CONFLICT DO UPDATE rather than select-then-insert: two
        # transcription-driven requests for one call can offer the same slot
        # concurrently, and a check-then-write would race into a unique
        # violation that poisons the request's transaction. Refreshing the
        # duration keeps the record aligned with the visit length most
        # recently quoted.
        #
        # `offered_turn_index` and the selection columns are deliberately NOT
        # in the update set. Re-offering a time the caller already chose must
        # not clear their choice, and must not advance the index that made
        # that choice legitimate in the first place — a later re-offer would
        # otherwise retroactively invalidate a real selection.
        statement = pg_insert(OfferedSlotModel).values(
            [
                {
                    "id": uuid.uuid4(),
                    "organization_id": organization_id,
                    "conversation_id": conversation_id,
                    "slot_start_at": slot.start_at,
                    "duration_minutes": slot.duration_minutes,
                    "offered_turn_index": turn_index,
                }
                for slot in slots
            ]
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_offered_slot_conversation_start",
                set_={"duration_minutes": statement.excluded.duration_minutes},
            )
        )
        await self._session.flush()

    async def list_offered_starts(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[datetime]:
        result = await self._session.execute(
            select(OfferedSlotModel.slot_start_at)
            .where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
            )
            .order_by(OfferedSlotModel.slot_start_at.asc())
        )
        return list(result.scalars().all())

    async def offered_duration_minutes(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
    ) -> int | None:
        result = await self._session.execute(
            select(OfferedSlotModel.duration_minutes).where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
                OfferedSlotModel.slot_start_at == start_at,
            )
        )
        return result.scalar_one_or_none()

    async def get_offered(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
    ) -> OfferedSlot | None:
        result = await self._session.execute(
            select(OfferedSlotModel).where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
                OfferedSlotModel.slot_start_at == start_at,
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def mark_selected(
        self,
        organization_id: uuid.UUID,
        conversation_id: uuid.UUID,
        start_at: datetime,
        turn_index: int,
    ) -> OfferedSlot | None:
        # Clear first, then set — in that order and in one transaction, so the
        # partial unique index over selected rows is never transiently
        # violated by a second selection existing alongside the first.
        await self.clear_selection(organization_id, conversation_id)

        selected_at = datetime.now(timezone.utc)
        await self._session.execute(
            update(OfferedSlotModel)
            .where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
                OfferedSlotModel.slot_start_at == start_at,
            )
            .values(selected_at=selected_at, selected_turn_index=turn_index)
        )
        await self._session.flush()
        # Read back rather than RETURNING: an UPDATE that matched nothing and
        # one that matched a row have to be distinguishable here (the caller
        # turns None into NOT_OFFERED), and a plain re-select says that
        # without depending on ORM RETURNING semantics.
        return await self.get_offered(organization_id, conversation_id, start_at)

    async def get_active_selection(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> OfferedSlot | None:
        result = await self._session.execute(
            select(OfferedSlotModel).where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
                OfferedSlotModel.selected_at.is_not(None),
            )
        )
        model = result.scalar_one_or_none()
        return _to_entity(model) if model else None

    async def clear_selection(
        self, organization_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(OfferedSlotModel)
            .where(
                OfferedSlotModel.organization_id == organization_id,
                OfferedSlotModel.conversation_id == conversation_id,
                OfferedSlotModel.selected_at.is_not(None),
            )
            .values(selected_at=None, selected_turn_index=None)
        )
        await self._session.flush()


def _to_entity(model: OfferedSlotModel) -> OfferedSlot:
    return OfferedSlot(
        organization_id=model.organization_id,
        conversation_id=model.conversation_id,
        start_at=model.slot_start_at,
        duration_minutes=model.duration_minutes,
        offered_turn_index=model.offered_turn_index,
        selected_at=model.selected_at,
        selected_turn_index=model.selected_turn_index,
    )
